"""Embedding generation and vector retrieval.

Keeps two things separate that are easy to conflate: *producing* vectors for
content, and *searching* with them. Both live here so the pgvector query syntax
and the embedding-text composition stay next to each other and cannot drift.

Spec 54.09 s13 puts embeddings in the primary store rather than a separate vector
database, which is why this is a pgvector query rather than a network call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog.models import (
    STATUS_PUBLISHED,
    Experience,
)
from app.domains.catalog.repository import (
    DISCOVERABLE_MODERATION_STATUSES,
    published_experiences,
)
from app.integrations.embeddings import (
    TASK_DOCUMENT,
    TASK_QUERY,
    EmbeddingBatch,
    embedding_text_for_experience,
    get_embedding_provider,
)

logger = get_logger("mado.embeddings.service")

def max_cosine_distance() -> float:
    """The active provider's noise threshold.

    Read per call rather than captured in a constant: pgvector's `<=>` returns 0
    for identical and 2 for opposite, but *where unrelated content actually lands*
    on that scale is a property of the model. Hard-coding one number would silently
    filter nothing on one provider and over-filter on another.
    """
    return getattr(get_embedding_provider(), "max_distance", 0.75)


@dataclass(slots=True)
class VectorHit:
    experience_id: uuid.UUID
    distance: float

    @property
    def similarity(self) -> float:
        """Cosine similarity in 0-1, for blending with other relevance scores."""
        return max(0.0, 1.0 - (self.distance / 2.0))


def _text_for(experience: Experience) -> str:
    venue = experience.venue
    return embedding_text_for_experience(
        title=experience.title,
        summary=experience.summary,
        description=experience.description,
        category=experience.category.name if experience.category else None,
        tags=[tag.name for tag in (experience.tags or [])],
        venue=venue.name if venue else None,
        neighborhood=venue.neighborhood.name if venue and venue.neighborhood else None,
    )


async def embed_experience(session: AsyncSession, experience: Experience) -> bool:
    """Generate and store the vector for one experience.

    Returns whether a vector was written. Callers treat failure as non-fatal:
    an experience without an embedding is still fully discoverable by keyword.
    """
    provider = get_embedding_provider()
    try:
        batch = await provider.embed([_text_for(experience)], task=TASK_DOCUMENT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed_failed", experience_id=str(experience.id), error=str(exc))
        return False

    if not batch:
        return False
    experience.embedding = batch.vectors[0]
    # Tagged with whoever actually produced it, which is not necessarily the
    # configured provider: Gemini falls back to hashing during an outage.
    experience.embedding_model = batch.model
    return True


async def backfill_embeddings(
    session: AsyncSession, *, batch_size: int = 32, only_missing: bool = True
) -> int:
    """Embed published experiences in batches.

    Batched because providers charge and rate-limit per request, not per item.
    """
    provider = get_embedding_provider()
    stmt: Select = published_experiences()
    if only_missing:
        # "Missing" includes vectors from a different model: they are unusable for
        # retrieval under the current provider, so leaving them would look embedded
        # while contributing nothing.
        stmt = stmt.where(
            (Experience.embedding.is_(None))
            | (Experience.embedding_model.is_distinct_from(provider.name))
        )

    result = await session.execute(stmt)
    experiences = list(result.scalars().unique().all())
    if not experiences:
        return 0

    written = 0
    for start in range(0, len(experiences), batch_size):
        chunk = experiences[start : start + batch_size]
        try:
            batch = await provider.embed([_text_for(e) for e in chunk], task=TASK_DOCUMENT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed_batch_failed", error=str(exc), size=len(chunk))
            continue

        for experience, vector in zip(chunk, batch.vectors, strict=False):
            experience.embedding = vector
            experience.embedding_model = batch.model
            written += 1
        await session.flush()

    logger.info(
        "embedding_backfill_complete",
        written=written,
        provider=provider.name,
        semantic=provider.is_semantic,
    )
    return written


async def embed_query(text: str) -> EmbeddingBatch | None:
    """Embed a search query, using the query-side task type.

    Returns the whole batch, not just the vector: the caller needs the producing
    model to restrict the search to comparable rows.
    """
    provider = get_embedding_provider()
    try:
        batch = await provider.embed([text], task=TASK_QUERY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_embed_failed", error=str(exc))
        return None
    return batch if batch else None


async def vector_search(
    session: AsyncSession,
    query_vector: list[float],
    *,
    model: str | None = None,
    city_slug: str | None = None,
    limit: int = 60,
    max_distance: float | None = None,
) -> list[VectorHit]:
    """Nearest neighbours by cosine distance, respecting discovery visibility."""
    if max_distance is None:
        max_distance = max_cosine_distance()
    if model is None:
        model = get_embedding_provider().name
    distance = Experience.embedding.cosine_distance(query_vector).label("distance")

    stmt = (
        select(Experience.id, distance)
        .where(
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.moderation_status.in_(DISCOVERABLE_MODERATION_STATUSES),
            Experience.embedding.is_not(None),
            # Only rows in the same vector space as the query. Mixing models
            # produces distances that look valid and rank randomly.
            Experience.embedding_model == model,
        )
        .order_by(distance)
        .limit(limit)
    )

    if city_slug:
        from app.domains.catalog.models import City

        stmt = stmt.join(City, Experience.city_id == City.id).where(City.slug == city_slug)

    result = await session.execute(stmt)
    return [
        VectorHit(experience_id=row.id, distance=float(row.distance))
        for row in result
        if row.distance is not None and float(row.distance) <= max_distance
    ]


def prune_to_best(hits: list[VectorHit], *, margin: float = 0.06) -> list[VectorHit]:
    """Keep only neighbours close to the best one.

    An absolute cutoff cannot separate signal from noise here. Two effects fight
    it: embedding models have a similarity floor that differs per model, and this
    corpus is topically tight - every listing is a thing to do in one city, so any
    city-flavoured query is *somewhat* close to all of it. The result is that a
    fixed threshold either admits the whole catalogue or throws away real matches.

    A margin relative to the best hit sidesteps both. It asks "how much worse than
    the best match is this?", which is the question that actually matters and which
    needs no per-model tuning. A query with one strong answer returns a short list;
    a broad query where everything scores alike returns a long one - both correct.
    """
    if not hits:
        return []
    ceiling = hits[0].distance + margin
    return [hit for hit in hits if hit.distance <= ceiling]


async def similar_by_vector(
    session: AsyncSession, experience: Experience, *, limit: int = 12
) -> list[VectorHit]:
    """Semantically similar experiences, excluding the source."""
    if experience.embedding is None:
        return []
    hits = await vector_search(
        session,
        list(experience.embedding),
        # Compare against peers embedded by the same model as this row, whatever
        # the currently configured provider happens to be.
        model=experience.embedding_model,
        city_slug=None,
        limit=limit + 1,
    )
    return [hit for hit in hits if hit.experience_id != experience.id][:limit]
