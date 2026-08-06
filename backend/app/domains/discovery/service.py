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
from app.domains.catalog.schemas import ExperienceSummary
from app.domains.catalog.serializers import to_summary
from app.domains.discovery.ranking import (
    RankingContext,
    rank,
    tonight_window,
    weekend_window,
)
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
    ) -> list[ExperienceSummary]:
        ranked = rank(experiences, ctx, relevance_by_id=relevance, diversify=diversify, limit=limit)
        return [
            to_summary(
                item.experience,
                reason=item.reason,
                distance_km=item.distance_km,
                is_saved=str(item.experience.id) in ctx.saved_experience_ids,
            )
            for item in ranked
        ]

    # ----------------------------------------------------------------- canvas

    async def build_canvas(
        self,
        ctx: RankingContext,
        *,
        city_slug: str,
        module_limit: int = 12,
    ) -> list[FeedModule]:
        """Assemble the Discovery Canvas.

        Module order is deliberate: time-critical rails ("Tonight") outrank
        evergreen ones, because spec PRODUCT-00 principle 15 optimises for the
        explorer with a few free hours right now.
        """
        modules: list[FeedModule] = []

        for_you = await self.for_you(ctx, city_slug=city_slug, limit=module_limit)
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

        tonight = await self.tonight(ctx, city_slug=city_slug, limit=module_limit)
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

        if ctx.has_location:
            nearby = await self.nearby(ctx, limit=module_limit)
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

        weekend = await self.weekend(ctx, city_slug=city_slug, limit=module_limit)
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

        free = await self.free_experiences(ctx, city_slug=city_slug, limit=module_limit)
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

        trending = await self.trending(ctx, city_slug=city_slug, limit=module_limit)
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

        hidden = await self.hidden_gems(ctx, city_slug=city_slug, limit=module_limit)
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
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session, city_slug=city_slug, limit=CANDIDATE_POOL
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def happening_now(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        """In progress or starting within the hour."""
        window = (ctx.now - timedelta(hours=2), ctx.now + timedelta(hours=1))
        candidates = await catalog_repo.query_experiences(
            self.session,
            city_slug=city_slug,
            starts_between=window,
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def tonight(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session,
            city_slug=city_slug,
            starts_between=tonight_window(ctx.now),
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def weekend(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session,
            city_slug=city_slug,
            starts_between=weekend_window(ctx.now),
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def nearby(
        self, ctx: RankingContext, *, radius_km: float = 5.0, limit: int = 12
    ) -> list[ExperienceSummary]:
        if not ctx.has_location:
            return []
        candidates = await catalog_repo.nearby_experiences(
            self.session,
            latitude=ctx.latitude,
            longitude=ctx.longitude,
            radius_km=radius_km,
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def trending(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session, city_slug=city_slug, limit=CANDIDATE_POOL
        )
        # Trend score is the point of this rail, so it is sorted on directly rather
        # than blended - but the full ranker still supplies reasons and distances.
        candidates.sort(key=lambda exp: float(exp.trend_score or 0), reverse=True)
        return self.summarize(candidates[: limit * 2], ctx, limit=limit, diversify=False)

    async def free_experiences(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session, city_slug=city_slug, free_only=True, limit=CANDIDATE_POOL
        )
        return self.summarize(candidates, ctx, limit=limit)

    async def hidden_gems(
        self, ctx: RankingContext, *, city_slug: str, limit: int = 12
    ) -> list[ExperienceSummary]:
        """High quality, low popularity - the serendipity rail (spec principle 14)."""
        candidates = await catalog_repo.query_experiences(
            self.session, city_slug=city_slug, limit=CANDIDATE_POOL
        )
        gems = [
            exp
            for exp in candidates
            if float(exp.quality_score or 0) >= 0.65 and float(exp.popularity_score or 0) <= 0.45
        ]
        return self.summarize(gems, ctx, limit=limit)

    async def by_category(
        self, ctx: RankingContext, *, city_slug: str, category_slug: str, limit: int = 24
    ) -> list[ExperienceSummary]:
        candidates = await catalog_repo.query_experiences(
            self.session,
            city_slug=city_slug,
            category_slugs=[category_slug],
            limit=CANDIDATE_POOL,
        )
        return self.summarize(candidates, ctx, limit=limit, diversify=False)

    async def similar_to(
        self, ctx: RankingContext, experience: Experience, *, limit: int = 12
    ) -> list[ExperienceSummary]:
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
        """Retrieve candidates from the index, then rank them here.

        The vendor's relevance is one input among seven (see
        :mod:`app.domains.discovery.ranking`), which is what keeps ranking ownership
        inside the platform per spec 82.01 s13.
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

        try:
            response = await self.search.search(query, filters=filters, limit=CANDIDATE_POOL)
        except SearchUnavailable:
            # Degrade to a database scan rather than failing the request outright.
            logger.warning("search_degraded_to_database", query=query)
            candidates = await catalog_repo.query_experiences(
                self.session,
                city_slug=city_slug,
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
            items = self.summarize(matched, ctx, limit=limit, diversify=False)
            return SearchOutcome(items=items, total=len(matched), query=query, degraded=True)

        ordered_ids: list[uuid.UUID] = []
        relevance: dict[str, float] = {}
        for hit in response.hits:
            try:
                identifier = uuid.UUID(hit.id)
            except ValueError:
                continue
            ordered_ids.append(identifier)
            relevance[hit.id] = hit.score

        experiences = await catalog_repo.get_experiences_by_ids(self.session, ordered_ids)
        items = self.summarize(experiences, ctx, limit=limit, relevance=relevance, diversify=False)
        return SearchOutcome(items=items, total=response.estimated_total, query=query)

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
) -> RankingContext:
    """Assemble a :class:`RankingContext` from request and profile inputs.

    Preference keys mirror the Living Explorer Profile shape written by the
    onboarding flow (spec 10.01.02).
    """
    preferences = preferences or {}
    return RankingContext(
        now=now or datetime.now(UTC),
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        preferred_categories=set(preferences.get("categories", []) or []),
        preferred_tags=set(preferences.get("tags", []) or []),
        disliked_categories=set(preferences.get("dislikedCategories", []) or []),
        budget=preferences.get("budget"),
        is_raining=is_raining,
        saved_experience_ids=saved_ids or set(),
    )
