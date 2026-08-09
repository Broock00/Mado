"""Search infrastructure adapter.

Spec 82.01 s9/s13 is explicit about the division of labour: **Meilisearch stores
indexes and returns candidates; Mado owns ranking, personalization and enrichment.**
So this module deliberately does *not* express business ranking - it retrieves a
candidate set and hands it to :mod:`app.domains.discovery.ranking`.

Everything vendor-specific stays behind :class:`SearchClient`, which is what makes
the documented swap to OpenSearch a single-file change.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from meilisearch_python_sdk import AsyncClient
from meilisearch_python_sdk.models.settings import MinWordSizeForTypos, TypoTolerance

from app.core import tracing
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("mado.search")

EXPERIENCES_INDEX = "experiences"

# Ordered by descending weight: Meilisearch treats earlier attributes as more
# important, which handles the "title beats description" case before our own
# ranking layer ever runs.
SEARCHABLE_ATTRIBUTES = [
    "title",
    "summary",
    "category_name",
    "tags",
    "venue_name",
    "neighborhood_name",
    "publisher_name",
    "description",
]

FILTERABLE_ATTRIBUTES = [
    "city_slug",
    "category_slug",
    "tags",
    "type",
    "price_type",
    "is_indoor",
    "is_free",
    "neighborhood_slug",
    "publisher_verified",
    "next_start_timestamp",
    "latitude",
    "longitude",
]

SORTABLE_ATTRIBUTES = [
    "popularity_score",
    "quality_score",
    "next_start_timestamp",
    "published_timestamp",
]


@dataclass(slots=True)
class SearchHit:
    id: str
    score: float
    document: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResponse:
    hits: list[SearchHit]
    estimated_total: int
    query: str
    # False when the vendor was unreachable and we fell back, so callers can tell
    # "nothing matched" apart from "search is degraded".
    served_by_index: bool = True


class SearchUnavailable(RuntimeError):
    """Raised when the search backend cannot serve a query."""


class SearchClient:
    """Thin, replaceable wrapper over the search vendor."""

    def __init__(self, url: str, api_key: str) -> None:
        self._url = url
        self._api_key = api_key
        self._client: AsyncClient | None = None

    def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(self._url, self._api_key)
        return self._client

    async def ensure_indexes(self) -> None:
        """Create the experiences index and apply settings. Idempotent."""
        client = self._get_client()
        index = client.index(EXPERIENCES_INDEX)
        # "Already exists" is the common, benign outcome on every run after the
        # first, so creation failure is not an error worth surfacing.
        with contextlib.suppress(Exception):
            await client.create_index(EXPERIENCES_INDEX, primary_key="id")

        await index.update_searchable_attributes(SEARCHABLE_ATTRIBUTES)
        await index.update_filterable_attributes(FILTERABLE_ATTRIBUTES)
        await index.update_sortable_attributes(SORTABLE_ATTRIBUTES)
        # Typo tolerance matters disproportionately here: venue and event names are
        # often transliterated Amharic, where spelling varies legitimately, and
        # many are short ("tibs", "shiro", "jebena", "Zoma").
        #
        # Thresholds are well below the 5/9 defaults because only the *final* term
        # of a query gets prefix matching - every earlier term must match on typo
        # tolerance alone. At the default, a three-letter term like "jaz" allows no
        # typos and kills the whole query. Three is the practical floor: lower, and
        # two-letter tokens start matching almost anything.
        await index.update_typo_tolerance(
            TypoTolerance(
                enabled=True,
                min_word_size_for_typos=MinWordSizeForTypos(one_typo=3, two_typos=7),
            )
        )
        logger.info("search_indexes_ready", index=EXPERIENCES_INDEX)

    async def index_documents(self, documents: list[dict[str, Any]]) -> None:
        if not documents:
            return
        index = self._get_client().index(EXPERIENCES_INDEX)
        await index.add_documents(documents, primary_key="id")
        logger.info("search_documents_indexed", count=len(documents))

    async def delete_document(self, document_id: str) -> None:
        index = self._get_client().index(EXPERIENCES_INDEX)
        await index.delete_document(document_id)

    async def clear(self) -> None:
        index = self._get_client().index(EXPERIENCES_INDEX)
        await index.delete_all_documents()

    async def search(
        self,
        query: str,
        *,
        filters: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchResponse:
        """Retrieve candidates. Ranking is applied by the caller, not here.

        ``limit`` is intentionally generous relative to the page size the user sees:
        the ranking layer needs a wider candidate pool than the final result count
        to do anything meaningful with diversity and personalization.
        """
        index = self._get_client().index(EXPERIENCES_INDEX)
        filter_expression = " AND ".join(filters) if filters else None

        try:
            with tracing.dependency("search"):
                result = await index.search(
                    query,
                    limit=limit,
                    offset=offset,
                    filter=filter_expression,
                    show_ranking_score=True,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_query_failed", error=str(exc), query=query)
            raise SearchUnavailable(str(exc)) from exc

        hits = [
            SearchHit(
                id=str(hit.get("id")),
                score=float(hit.get("_rankingScore", 0.0)),
                document=hit,
            )
            for hit in result.hits
        ]
        return SearchResponse(
            hits=hits,
            estimated_total=getattr(result, "estimated_total_hits", len(hits)) or len(hits),
            query=query,
        )

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Autocomplete over indexed titles.

        Meilisearch has no dedicated suggest endpoint, so a narrow, few-attribute
        search is the idiomatic approach and stays fast enough for keystroke latency.
        """
        index = self._get_client().index(EXPERIENCES_INDEX)
        try:
            result = await index.search(
                prefix,
                limit=limit,
                attributes_to_retrieve=["id", "title", "type", "category_name", "city_slug"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_suggest_failed", error=str(exc))
            return []
        return list(result.hits)

    async def health(self) -> bool:
        try:
            await self._get_client().health()
        except Exception:  # noqa: BLE001
            return False
        return True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@lru_cache
def get_search_client() -> SearchClient:
    settings = get_settings()
    return SearchClient(settings.meilisearch_url, settings.meilisearch_api_key)
