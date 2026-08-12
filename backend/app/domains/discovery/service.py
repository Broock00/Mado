"""Discovery service - the Discovery Canvas and search orchestration.

Spec 10.01.03 describes the canvas as "a dynamic decision-support interface", not a
feed: independently-ranked modules, each with its own eligibility rules, that are
hidden rather than shown empty. A module returning nothing is dropped, because an
empty rail is worse than one fewer rail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.models import Experience
from app.domains.catalog.repository import Area
from app.domains.catalog.schemas import ExperienceSummary
from app.domains.catalog.serializers import to_summary
from app.domains.discovery.embedding_service import (
    embed_query,
    prune_to_best,
    similar_by_vector,
    vector_search,
)
from app.domains.discovery.fusion import (
    KEYWORD_WEIGHT,
    VECTOR_WEIGHT,
    normalised_relevance,
    reciprocal_rank_fusion,
)
from app.domains.discovery.ranking import (
    SEARCH_WEIGHTS,
    RankingContext,
    rank,
    tonight_window,
    weekend_window,
)
from app.domains.explorer.learning import InferredPreferences
from app.integrations.search import SearchUnavailable, get_search_client

logger = get_logger("mado.discovery")

# Retrieve a wider candidate pool than we display: the ranking layer needs room to
# reorder, diversify and drop items, which it cannot do if retrieval already
# truncated to the page size.
CANDIDATE_POOL = 80


@dataclass(slots=True)
class FeedModule:
    key: str
    title: str
    subtitle: str | None
    layout: str  # carousel | grid | list | map
    items: list[ExperienceSummary]


@dataclass(slots=True)
class SearchOutcome:
    items: list[ExperienceSummary]
    total: int
    query: str
    # Signals that the vendor was unreachable and results came from Postgres, so
    # the UI can be honest about degraded relevance (spec 55.01 s40).
    degraded: bool = False
    # True when vector retrieval contributed, so the client can distinguish
    # "matched your words" from "understood what you meant".
    semantic: bool = False


class DiscoveryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.search = get_search_client()

    # ---------------------------------------------------------------- helpers

    def summarize(
        self,
        experiences: list[Experience],
        ctx: RankingContext,
        *,
        limit: int,
        relevance: dict[str, float] | None = None,
        diversify: bool = True,
        weights: dict[str, float] | None = None,
    ) -> list[ExperienceSummary]:
        ranked = rank(
            experiences,
            ctx,
            relevance_by_id=relevance,
            diversify=diversify,
            limit=limit,
            weights=weights,
        )
        return [
            to_summary(
                item.experience,
                reason=item.reason,
                distance_km=item.distance_km,
                is_saved=str(item.experience.id) in ctx.saved_experience_ids,
            )
            for item in ranked
        ]

    async def _pool(self, *, area: Area, **filters) -> list:
        """Candidates for one rail, looking close in before looking further out.

        "What is happening near you" should mean the next street before it means
        the far side of the city. So the smallest radius is tried first and the
        search only widens when there is not enough nearby to rank into
        anything - an empty answer at two kilometres is not an empty city.

        Widening is not the same as ignoring distance: the ranker scores
        proximity, so a wider pool still puts the closest things first. This
        only decides what it is allowed to consider.
        """
        found: list = []
        for step in area.ladder():
            found = await catalog_repo.query_experiences(
                self.session, area=step, limit=CANDIDATE_POOL, **filters
            )
            if len(found) >= catalog_repo.MIN_CANDIDATES:
                break
        return found

    # ----------------------------------------------------------------- canvas

    async def build_canvas(
        self,
        ctx: RankingContext,
        *,
        area: Area,
        module_limit: int = 12,
    ) -> list[FeedModule]:
        """Assemble the Discovery Canvas.

        Module order is deliberate: time-critical rails ("Tonight") outrank
        evergreen ones, because spec PRODUCT-00 principle 15 optimises for the
        explorer with a few free hours right now.
        """
        modules: list[FeedModule] = []

        for_you = await self.for_you(ctx, area=area, limit=module_limit)
        if for_you:
            modules.append(
                FeedModule(
                    key="for_you",
                    title="For you",
                    subtitle="Picked from what you like and where you are",
                    layout="carousel",
                    items=for_you,
                )
            )

        tonight = await self.tonight(ctx, area=area, limit=module_limit)
        if tonight:
            modules.append(
                FeedModule(
                    key="tonight",
                    title="Tonight",
                    subtitle="Still time to make it",
                    layout="carousel",
                    items=tonight,
                )
            )

        if ctx.has_location or (area is not None and area.has_point):
            nearby = await self.nearby(ctx, area=area, limit=module_limit)
            if nearby:
                modules.append(
                    FeedModule(
                        key="nearby",
                        title="Near you",
                        subtitle="Within walking or a short ride",
                        layout="carousel",
                        items=nearby,
                    )
                )

        weekend = await self.weekend(ctx, area=area, limit=module_limit)
        if weekend:
            modules.append(
                FeedModule(
                    key="weekend",
                    title="This weekend",
                    subtitle="Worth planning ahead for",
                    layout="carousel",
                    items=weekend,
                )
            )

        free = await self.free_experiences(ctx, area=area, limit=module_limit)
        if free:
            modules.append(
                FeedModule(
                    key="free",
                    title="Free to do",
                    subtitle="Good days out that cost nothing",
                    layout="carousel",
                    items=free,
                )
            )

        trending = await self.trending(ctx, area=area, limit=module_limit)
        if trending:
            modules.append(
                FeedModule(
                    key="trending",
                    title="Trending in the city",
                    subtitle="What people are drawn to right now",
                    layout="carousel",
                    items=trending,
                )
            )

        hidden = await self.hidden_gems(ctx, area=area, limit=module_limit)
        if hidden:
            modules.append(
                FeedModule(
                    key="hidden_gems",
                    title="Hidden gems",
                    subtitle="Well loved, not yet crowded",
                    layout="carousel",
                    items=hidden,
                )
            )

        return modules

    # ---------------------------------------------------------------- modules

    async def for_you(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area)
        return self.summarize(candidates, ctx, limit=limit)

    async def happening_now(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        """In progress or starting within the hour."""
        window = (ctx.now - timedelta(hours=2), ctx.now + timedelta(hours=1))
        candidates = await self._pool(area=area, starts_between=window)
        return self.summarize(candidates, ctx, limit=limit)

    async def tonight(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area, starts_between=tonight_window(ctx.now))
        return self.summarize(candidates, ctx, limit=limit)

    async def weekend(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area, starts_between=weekend_window(ctx.now))
        return self.summarize(candidates, ctx, limit=limit)

    async def nearby(
        self,
        ctx: RankingContext,
        *,
        area: Area | None = None,
        radius_km: float = 5.0,
        limit: int = 12,
    ) -> list[ExperienceSummary]:
        """Close to the area being looked at, which is not always the explorer.

        Somebody in Addis searching "Brooklyn" is asking about Brooklyn. Reading
        the coordinates off the ranking context instead would answer with things
        near them - the one rail on the page confidently showing the wrong
        continent, under a heading that says "near you".
        """
        point = (
            (area.latitude, area.longitude)
            if area is not None and area.has_point
            else (ctx.latitude, ctx.longitude)
            if ctx.has_location
            else None
        )
        if point is None:
            return []

        candidates = await catalog_repo.nearby_experiences(
            self.session,
            latitude=point[0],
            longitude=point[1],
            radius_km=(area.radius_km if area is not None and area.has_point else None)
            or radius_km,
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def trending(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area)
        # Trend score is the point of this rail, so it is sorted on directly rather
        # than blended - but the full ranker still supplies reasons and distances.
        candidates.sort(key=lambda exp: float(exp.trend_score or 0), reverse=True)
        return self.summarize(candidates[: limit * 2], ctx, limit=limit, diversify=False)

    async def free_experiences(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area, free_only=True)
        return self.summarize(candidates, ctx, limit=limit)

    async def hidden_gems(
        self, ctx: RankingContext, *, area: Area, limit: int = 12
    ) -> list[ExperienceSummary]:
        """High quality, low popularity - the serendipity rail (spec principle 14)."""
        candidates = await self._pool(area=area)
        gems = [
            exp
            for exp in candidates
            if float(exp.quality_score or 0) >= 0.65 and float(exp.popularity_score or 0) <= 0.45
        ]
        return self.summarize(gems, ctx, limit=limit)

    async def by_category(
        self, ctx: RankingContext, *, area: Area, category_slug: str, limit: int = 24
    ) -> list[ExperienceSummary]:
        candidates = await self._pool(area=area, category_slugs=[category_slug])
        return self.summarize(candidates, ctx, limit=limit, diversify=False)

    async def similar_to(
        self, ctx: RankingContext, experience: Experience, *, limit: int = 12
    ) -> list[ExperienceSummary]:
        """Related experiences, preferring semantic neighbours.

        Vector similarity understands that a coffee ceremony and a roastery visit
        are related even when they share no category or tag. The relational query
        remains the fallback for anything not yet embedded.
        """
        vector_hits = await similar_by_vector(self.session, experience, limit=CANDIDATE_POOL)
        if vector_hits:
            ordered = [hit.experience_id for hit in vector_hits]
            candidates = await catalog_repo.get_experiences_by_ids(self.session, ordered)
            relevance = {str(hit.experience_id): hit.similarity for hit in vector_hits}
            if candidates:
                return self.summarize(
                    candidates, ctx, limit=limit, relevance=relevance, diversify=False
                )

        candidates = await catalog_repo.similar_experiences(
            self.session, experience, limit=CANDIDATE_POOL
        )
        return self.summarize(candidates, ctx, limit=limit, diversify=False)

    # ----------------------------------------------------------------- search

    async def search_experiences(
        self,
        query: str,
        ctx: RankingContext,
        *,
        city_slug: str | None = None,
        category_slugs: list[str] | None = None,
        free_only: bool = False,
        experience_type: str | None = None,
        limit: int = 24,
    ) -> SearchOutcome:
        """Hybrid retrieval, then platform ranking.

        Two retrievers run against the same query and their results are fused by
        rank (see :mod:`app.domains.discovery.fusion`):

        * **keyword** via the search index - exact names, rare tokens
        * **vector** via pgvector - paraphrase and intent

        The fused relevance is then one input among seven to the ranker, which is
        what keeps ranking ownership inside the platform per spec 82.01 s13.

        Each retriever is optional. If the index is down we fall back to the
        database; if embeddings are missing the keyword ranking simply stands
        alone. Search degrades, it does not break.
        """
        filters: list[str] = []
        if city_slug:
            filters.append(f'city_slug = "{city_slug}"')
        if category_slugs:
            joined = " OR ".join(f'category_slug = "{slug}"' for slug in category_slugs)
            filters.append(f"({joined})")
        if free_only:
            filters.append("is_free = true")
        if experience_type:
            filters.append(f'type = "{experience_type}"')

        # Vector retrieval runs regardless of whether the keyword index is
        # reachable, so semantic search survives a Meilisearch outage.
        vector_ids = await self._vector_candidates(query, city_slug=city_slug)

        try:
            response = await self.search.search(query, filters=filters, limit=CANDIDATE_POOL)
        except SearchUnavailable:
            # The keyword index is unreachable. Vector results may still be
            # available, and are far better than a substring scan.
            logger.warning("keyword_search_unavailable", query=query, vector_hits=len(vector_ids))
            if vector_ids:
                experiences = await catalog_repo.get_experiences_by_ids(self.session, vector_ids)
                fused = reciprocal_rank_fusion({"vector": vector_ids})
                items = self.summarize(
                    experiences,
                    ctx,
                    limit=limit,
                    relevance=normalised_relevance(fused),
                    diversify=False,
                    weights=SEARCH_WEIGHTS,
                )
                return SearchOutcome(
                    items=items, total=len(experiences), query=query, degraded=True
                )

            candidates = await catalog_repo.query_experiences(
                self.session,
                area=Area(city_slug=city_slug) if city_slug else None,
                category_slugs=category_slugs,
                free_only=free_only,
                experience_type=experience_type,
                limit=CANDIDATE_POOL,
            )
            needle = query.casefold()
            matched = [
                exp
                for exp in candidates
                if needle in exp.title.casefold()
                or (exp.summary and needle in exp.summary.casefold())
                or needle in exp.description.casefold()
            ]
            items = self.summarize(
                matched, ctx, limit=limit, diversify=False, weights=SEARCH_WEIGHTS
            )
            return SearchOutcome(items=items, total=len(matched), query=query, degraded=True)

        keyword_ids: list[uuid.UUID] = []
        for hit in response.hits:
            try:
                keyword_ids.append(uuid.UUID(hit.id))
            except ValueError:
                continue

        ranked_lists = {"keyword": keyword_ids}
        if vector_ids:
            ranked_lists["vector"] = vector_ids

        fused = reciprocal_rank_fusion(
            ranked_lists,
            weights={"keyword": KEYWORD_WEIGHT, "vector": VECTOR_WEIGHT},
            limit=CANDIDATE_POOL,
        )
        ordered_ids = [hit.experience_id for hit in fused]

        experiences = await catalog_repo.get_experiences_by_ids(self.session, ordered_ids)
        items = self.summarize(
            experiences,
            ctx,
            limit=limit,
            relevance=normalised_relevance(fused),
            diversify=False,
            # The explorer typed what they want; relevance leads here, unlike the feed.
            weights=SEARCH_WEIGHTS,
        )
        # Semantic hits the keyword index never saw are genuine extra results, so
        # the reported total has to account for them.
        total = max(response.estimated_total, len(ordered_ids))
        return SearchOutcome(items=items, total=total, query=query, semantic=bool(vector_ids))

    async def _vector_candidates(
        self, query: str, *, city_slug: str | None
    ) -> list[uuid.UUID]:
        """Nearest neighbours for the query, or an empty list if unavailable."""
        if not query.strip():
            return []
        batch = await embed_query(query)
        if batch is None:
            return []
        try:
            hits = await vector_search(
                self.session,
                batch.vectors[0],
                # The query's own producer, so a mid-flight provider fallback
                # searches the space it actually embedded into.
                model=batch.model,
                max_distance=batch.max_distance,
                city_slug=city_slug,
                limit=CANDIDATE_POOL,
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never fail the request
            logger.warning("vector_search_failed", error=str(exc))
            return []
        # Trim to neighbours comparable with the best one. Without this, a tight
        # topical corpus makes nearly every listing a "match" for any query, which
        # inflates the reported total and pads the tail with unrelated results.
        return [hit.experience_id for hit in prune_to_best(hits)]

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[dict]:
        if not prefix.strip():
            return []
        hits = await self.search.suggest(prefix, limit=limit)
        return [
            {
                "id": hit.get("id"),
                "title": hit.get("title"),
                "type": hit.get("type"),
                "category": hit.get("category_name"),
            }
            for hit in hits
        ]


def build_context(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    preferences: dict | None = None,
    saved_ids: set[str] | None = None,
    is_raining: bool = False,
    now: datetime | None = None,
    timezone: str = "Africa/Addis_Ababa",
    inferred: InferredPreferences | None = None,
) -> RankingContext:
    """Assemble a :class:`RankingContext` from request and profile inputs.

    Preference keys mirror the Living Explorer Profile shape written by the
    onboarding flow (spec 10.01.02).

    ``inferred`` is passed in rather than loaded here so this stays a pure
    function: it reads a database, and building a ranking context should not.
    Callers fetch it once per request via :func:`infer_preferences`.
    """
    preferences = preferences or {}
    stated_dislikes = set(preferences.get("dislikedCategories", []) or [])
    return RankingContext(
        now=now or datetime.now(UTC),
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        preferred_categories=set(preferences.get("categories", []) or []),
        preferred_tags=set(preferences.get("tags", []) or []),
        # Stated and learned dislikes are unioned rather than kept apart. Both mean
        # "do not show me this", and unlike positive affinities there is no case for
        # weighting a learned one less: repeatedly dismissing a category is about as
        # clear as ticking a box.
        disliked_categories=stated_dislikes
        | (inferred.disliked_categories if inferred else set()),
        inferred_categories=inferred.categories if inferred else {},
        inferred_tags=inferred.tags if inferred else {},
        inference_confidence=inferred.confidence if inferred else 0.0,
        budget=preferences.get("budget"),
        is_raining=is_raining,
        saved_experience_ids=saved_ids or set(),
    )
