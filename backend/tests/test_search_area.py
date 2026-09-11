"""Search must respect where the explorer asked to look.

The search index filters by city slug only. Before ``search_in_area``, a USA
pick reached GET /search as country=US, built an Area, and was then discarded -
Meilisearch ran unscoped and returned keyword hits from Addis, London and
everywhere else. These tests lock the branching that stops that happening again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.domains.catalog.repository import Area
from app.domains.discovery.ranking import RankingContext
from app.domains.discovery.service import DiscoveryService


def _ctx() -> RankingContext:
    return RankingContext(now=datetime.now(UTC))


@pytest.mark.asyncio
async def test_country_search_goes_through_geometry_not_the_index(monkeypatch):
    """A country area must never call the city-slug Meili path."""
    service = DiscoveryService(session=MagicMock())
    geometric = AsyncMock(
        return_value=SimpleNamespace(
            items=[],
            total=0,
            query="coffee",
            degraded=False,
            semantic=False,
            candidate_ids=set(),
        )
    )
    indexed = AsyncMock()
    monkeypatch.setattr(service, "_search_experiences_in_geometry", geometric)
    monkeypatch.setattr(service, "search_experiences", indexed)

    await service.search_in_area(
        "coffee",
        _ctx(),
        area=Area(latitude=39.8, longitude=-98.5, country_code="US"),
        city_slug=None,
        limit=12,
    )

    geometric.assert_awaited_once()
    indexed.assert_not_awaited()
    assert geometric.await_args.kwargs["area"].country_code == "US"


@pytest.mark.asyncio
async def test_city_search_still_uses_the_index(monkeypatch):
    service = DiscoveryService(session=MagicMock())
    geometric = AsyncMock()
    indexed = AsyncMock(
        return_value=SimpleNamespace(
            items=[],
            total=0,
            query="coffee",
            degraded=False,
            semantic=False,
            candidate_ids=set(),
        )
    )
    monkeypatch.setattr(service, "_search_experiences_in_geometry", geometric)
    monkeypatch.setattr(service, "search_experiences", indexed)

    await service.search_in_area(
        "coffee",
        _ctx(),
        area=Area(city_slug="addis-ababa"),
        city_slug="addis-ababa",
        limit=12,
    )

    indexed.assert_awaited_once()
    geometric.assert_not_awaited()
    assert indexed.await_args.kwargs["city_slug"] == "addis-ababa"


@pytest.mark.asyncio
async def test_point_radius_uses_geometry_even_when_a_city_was_resolved(monkeypatch):
    """Near-me is a circle. Replacing it with a city_slug filter would search
    the whole city (or, with no city, the whole world)."""
    service = DiscoveryService(session=MagicMock())
    geometric = AsyncMock(
        return_value=SimpleNamespace(
            items=[],
            total=0,
            query="coffee",
            degraded=False,
            semantic=False,
            candidate_ids=set(),
        )
    )
    indexed = AsyncMock()
    monkeypatch.setattr(service, "_search_experiences_in_geometry", geometric)
    monkeypatch.setattr(service, "search_experiences", indexed)

    await service.search_in_area(
        "coffee",
        _ctx(),
        area=Area(latitude=7.5356, longitude=37.8525, radius_km=5.0),
        city_slug="addis-ababa",
        limit=12,
    )

    geometric.assert_awaited_once()
    indexed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unscoped_search_still_hits_the_index(monkeypatch):
    """No place chosen remains an intentional global keyword search."""
    service = DiscoveryService(session=MagicMock())
    geometric = AsyncMock()
    indexed = AsyncMock(
        return_value=SimpleNamespace(
            items=[],
            total=0,
            query="coffee",
            degraded=False,
            semantic=False,
            candidate_ids=set(),
        )
    )
    monkeypatch.setattr(service, "_search_experiences_in_geometry", geometric)
    monkeypatch.setattr(service, "search_experiences", indexed)

    await service.search_in_area("coffee", _ctx(), area=None, city_slug=None, limit=12)

    indexed.assert_awaited_once()
    assert indexed.await_args.kwargs["city_slug"] is None
    geometric.assert_not_awaited()


@pytest.mark.asyncio
async def test_geometry_path_matches_words_only_inside_the_area(monkeypatch):
    """Listings outside the Area must not survive just because the title matches."""
    inside = SimpleNamespace(
        id=uuid4(),
        title="Coffee ceremony in Brooklyn",
        summary="A quiet cup",
        description="Roasters on the waterfront",
    )
    service = DiscoveryService(session=MagicMock())
    query_experiences = AsyncMock(return_value=[inside])
    monkeypatch.setattr(
        "app.domains.discovery.service.catalog_repo.query_experiences",
        query_experiences,
    )
    monkeypatch.setattr(
        service,
        "summarize",
        lambda experiences, ctx, **kwargs: [
            SimpleNamespace(id=exp.id, title=exp.title) for exp in experiences
        ],
    )

    area = Area(country_code="US")
    outcome = await service._search_experiences_in_geometry(
        "coffee",
        _ctx(),
        area=area,
        limit=12,
    )

    assert query_experiences.await_args.kwargs["area"] is area
    assert outcome.total == 1
    assert outcome.items[0].id == inside.id
    assert outcome.degraded is False
