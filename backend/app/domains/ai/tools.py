"""Tool registry.

Spec 56.03 s10-19 requires every tool to declare its side-effect class and whether
it needs confirmation, and spec 56.05 forbids hidden side effects. The registry
enforces that structurally: a tool that mutates state cannot execute through
:func:`execute_tool` without a confirmation token, regardless of what the model
decided to call.

Tool results are authoritative and the model's output is not (spec 56.03 s34) -
which is why results are returned as structured records the response layer renders,
never as prose the model is free to reinterpret.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionDeniedError
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.serializers import next_event_of
from app.domains.discovery.ranking import RankingContext
from app.domains.discovery.service import DiscoveryService

# Side-effect classes (spec 56.03 s18).
SIDE_EFFECT_NONE = "none"  # pure read
SIDE_EFFECT_LOCAL = "local"  # changes the explorer's own data
SIDE_EFFECT_EXTERNAL = "external"  # money, messages, third parties

# Risk levels (spec 56.01 s34).
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    side_effect: str
    risk: str
    requires_confirmation: bool
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]


@dataclass(slots=True)
class ToolResult:
    tool: str
    ok: bool
    items: list[dict[str, Any]] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    error: str | None = None
    # Spec 56.03 s35-37: results carry freshness so the response layer can hedge
    # on data that ages quickly.
    freshness: str = "live"
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


_REGISTRY: dict[str, ToolDefinition] = {}


def register(definition: ToolDefinition) -> None:
    _REGISTRY[definition.name] = definition


def get_tool(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolDefinition]:
    return list(_REGISTRY.values())


def describe_tools() -> list[dict[str, Any]]:
    """Provider-agnostic tool declarations for function calling."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in _REGISTRY.values()
    ]


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    confirmed: bool = False,
) -> ToolResult:
    """Run a registered tool, enforcing the confirmation policy.

    The check lives here rather than in each handler so that adding a mutating tool
    cannot accidentally skip it.
    """
    tool = get_tool(name)
    if tool is None:
        return ToolResult(tool=name, ok=False, error=f"Unknown tool '{name}'.")

    if tool.requires_confirmation and not confirmed:
        raise PermissionDeniedError(
            f"'{tool.name}' changes something, so it needs your confirmation first.",
            code="CONFIRMATION_REQUIRED",
            details={"tool": tool.name, "sideEffect": tool.side_effect, "risk": tool.risk},
        )

    return await tool.handler(session=session, ctx=ctx, city_slug=city_slug, **arguments)


# --------------------------------------------------------------------- helpers


def _to_item(summary, *, timezone: str = "Africa/Addis_Ababa") -> dict[str, Any]:
    """Shape a ranked experience for both the model and the client.

    ``when`` and ``price`` are pre-rendered so the offline provider can state them
    without doing any formatting - and so the model is never asked to compute a
    fact it could get wrong.

    Times are converted to the city's clock. Rendering the stored UTC value would
    report an Addis event three hours early, and the model has no way to detect
    that the string it was handed is wrong.
    """
    when = None
    if summary.next_event is not None:
        try:
            local_start = summary.next_event.start_time.astimezone(ZoneInfo(timezone))
        except Exception:  # noqa: BLE001 - unknown zone must not break a reply
            local_start = summary.next_event.start_time
        when = local_start.strftime("%a %d %b, %H:%M")

    if summary.price.type == "free":
        price = "Free"
    elif summary.price.amount is not None:
        price = f"{summary.price.amount:.0f} {summary.price.currency}"
    else:
        price = None

    return {
        "id": str(summary.id),
        "title": summary.title,
        "summary": summary.summary,
        "type": summary.type,
        "category": summary.category.name if summary.category else None,
        "venue_name": summary.venue.name if summary.venue else None,
        "neighborhood": summary.venue.neighborhood.name
        if summary.venue and summary.venue.neighborhood
        else None,
        "when": when,
        "price": price,
        "reason": summary.reason,
        "distance_km": summary.distance_km,
        "rating": summary.rating_average,
    }


# ----------------------------------------------------------------- tool bodies


async def _search_experiences(
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    query: str = "",
    categories: list[str] | None = None,
    free_only: bool = False,
    limit: int = 8,
) -> ToolResult:
    service = DiscoveryService(session)
    if query.strip():
        outcome = await service.search_experiences(
            query,
            ctx,
            city_slug=city_slug,
            category_slugs=categories,
            free_only=free_only,
            limit=limit,
        )
        items = outcome.items
    else:
        # No query text means "show me things like this", which the browse path
        # serves better than an empty full-text search.
        experiences = await catalog_repo.query_experiences(
            session,
            city_slug=city_slug,
            category_slugs=categories,
            free_only=free_only,
            limit=60,
        )
        items = service.summarize(experiences, ctx, limit=limit)

    return ToolResult(
        tool="search_experiences",
        ok=True,
        items=[_to_item(item, timezone=ctx.timezone) for item in items],
        entity_ids=[str(item.id) for item in items],
    )


async def _find_events(
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    starts_after: str | None = None,
    starts_before: str | None = None,
    categories: list[str] | None = None,
    free_only: bool = False,
    limit: int = 8,
) -> ToolResult:
    start = datetime.fromisoformat(starts_after) if starts_after else ctx.now
    end = datetime.fromisoformat(starts_before) if starts_before else None

    experiences = await catalog_repo.query_experiences(
        session,
        city_slug=city_slug,
        category_slugs=categories,
        free_only=free_only,
        starts_between=(start, end) if end else None,
        limit=60,
    )
    if end is None:
        experiences = [exp for exp in experiences if next_event_of(exp, now=start) is not None]

    service = DiscoveryService(session)
    items = service.summarize(experiences, ctx, limit=limit)
    return ToolResult(
        tool="find_events",
        ok=True,
        items=[_to_item(item, timezone=ctx.timezone) for item in items],
        entity_ids=[str(item.id) for item in items],
        # Schedules change; the response layer should not present these as settled.
        freshness="volatile",
    )


async def _find_nearby(
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    radius_km: float = 3.0,
    limit: int = 8,
) -> ToolResult:
    if not ctx.has_location:
        return ToolResult(
            tool="find_nearby",
            ok=False,
            error="No location available. Ask the explorer to share their location.",
        )
    service = DiscoveryService(session)
    items = await service.nearby(ctx, radius_km=radius_km, limit=limit)
    return ToolResult(
        tool="find_nearby",
        ok=True,
        items=[_to_item(item, timezone=ctx.timezone) for item in items],
        entity_ids=[str(item.id) for item in items],
    )


async def _get_experience_details(
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    experience_id: str,
) -> ToolResult:
    import uuid as _uuid

    try:
        identifier = _uuid.UUID(experience_id)
    except ValueError:
        return ToolResult(tool="get_experience_details", ok=False, error="Invalid experience id.")

    experience = await catalog_repo.get_experience(session, identifier)
    if experience is None:
        return ToolResult(
            tool="get_experience_details", ok=False, error="That experience is not available."
        )

    service = DiscoveryService(session)
    summary = service.summarize([experience], ctx, limit=1, diversify=False)
    detail = _to_item(summary[0], timezone=ctx.timezone) if summary else {}
    detail["description"] = experience.description
    detail["accessibility"] = experience.accessibility or {}
    return ToolResult(
        tool="get_experience_details",
        ok=True,
        items=[detail],
        entity_ids=[str(experience.id)],
    )


register(
    ToolDefinition(
        name="search_experiences",
        description=(
            "Search published experiences in the current city by free text and optional "
            "category or price filters. Use for 'find me...', 'where can I...' requests."
        ),
        side_effect=SIDE_EFFECT_NONE,
        risk=RISK_LOW,
        requires_confirmation=False,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search terms."},
                "categories": {"type": "array", "items": {"type": "string"}},
                "free_only": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
        handler=_search_experiences,
    )
)

register(
    ToolDefinition(
        name="find_events",
        description=(
            "Find scheduled events in a time window. Use whenever the request refers to "
            "a time such as tonight, tomorrow or this weekend."
        ),
        side_effect=SIDE_EFFECT_NONE,
        risk=RISK_LOW,
        requires_confirmation=False,
        parameters={
            "type": "object",
            "properties": {
                "starts_after": {"type": "string", "description": "ISO 8601 datetime."},
                "starts_before": {"type": "string", "description": "ISO 8601 datetime."},
                "categories": {"type": "array", "items": {"type": "string"}},
                "free_only": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
        handler=_find_events,
    )
)

register(
    ToolDefinition(
        name="find_nearby",
        description="Find experiences near the explorer's current location.",
        side_effect=SIDE_EFFECT_NONE,
        risk=RISK_LOW,
        requires_confirmation=False,
        parameters={
            "type": "object",
            "properties": {
                "radius_km": {"type": "number", "minimum": 0.2, "maximum": 25},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
        handler=_find_nearby,
    )
)

register(
    ToolDefinition(
        name="get_experience_details",
        description="Fetch full details for one experience by id.",
        side_effect=SIDE_EFFECT_NONE,
        risk=RISK_LOW,
        requires_confirmation=False,
        parameters={
            "type": "object",
            "properties": {"experience_id": {"type": "string"}},
            "required": ["experience_id"],
        },
        handler=_get_experience_details,
    )
)
