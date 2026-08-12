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

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionDeniedError
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.repository import Area
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
    # Structured output for tools whose result is more than a list of cards.
    # A plan has an order, timings and travel between stops - flattening it to
    # cards throws away exactly what makes it a plan rather than a list.
    payload: dict[str, Any] | None = None


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


# Tools that are about a place, and cannot honestly answer without one. Looking
# something up by id is not one of them.
#
# Without this guard an explorer in Paris asking "what is on tonight?" was handed
# eight listings five thousand kilometres away - the catalogue searched unscoped
# because no city resolved. Quieter than the old bug, which at least named the
# city it was wrong about, and worse for it.
NEEDS_A_CITY = frozenset(
    {"search_experiences", "find_events", "find_nearby", "plan_outing"}
)


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str | None,
    confirmed: bool = False,
) -> ToolResult:
    """Run a registered tool, enforcing the confirmation policy.

    The check lives here rather than in each handler so that adding a mutating tool
    cannot accidentally skip it.
    """
    tool = get_tool(name)
    if tool is None:
        return ToolResult(tool=name, ok=False, error=f"Unknown tool '{name}'.")

    if city_slug is None and name in NEEDS_A_CITY:
        return ToolResult(
            tool=name,
            ok=False,
            error=(
                "I do not know which city you are in yet. Share your location or "
                "pick a city, and I will look."
            ),
        )

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
            area=Area(city_slug=city_slug) if city_slug else None,
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
        area=Area(city_slug=city_slug) if city_slug else None,
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


async def _plan_outing(
    *,
    session: AsyncSession,
    ctx: RankingContext,
    city_slug: str,
    starts_after: str | None = None,
    starts_before: str | None = None,
    budget: float | None = None,
    max_stops: int = 3,
    categories: list[str] | None = None,
    free_only: bool = False,
    keep: list[str] | None = None,
    avoid: list[str] | None = None,
) -> ToolResult:
    """Build an actual itinerary rather than a list of candidates.

    This is the tool that makes PLAN_ACTIVITY mean something. Before it existed,
    "plan me an evening" ran a search and the model was left to invent an order and
    some timings - which is precisely the fact-inventing that spec 56.01 s3.1
    forbids. Now the ordering, the travel allowance and the timings are computed,
    and the model only has to phrase them.
    """
    from app.domains.explorer.planning import PlanRequest
    from app.domains.explorer.planning_service import PlanningService

    start = _parse_time(starts_after) or ctx.now
    end = _parse_time(starts_before) or (start + timedelta(hours=5))
    if end <= start:
        end = start + timedelta(hours=5)

    request = PlanRequest(
        start=start,
        end=end,
        city_slug=city_slug,
        latitude=ctx.latitude,
        longitude=ctx.longitude,
        budget=budget,
        max_stops=max(1, min(6, max_stops)),
        categories=categories or [],
        free_only=free_only,
        # Refinement (spec AI-004). Stops the explorer already accepted, and
        # ones they turned down.
        keep_experience_ids=_as_uuids(keep),
        avoid_experience_ids=_as_uuids(avoid),
    )

    plan = await PlanningService(session).plan(request, ctx)
    if plan.is_empty:
        return ToolResult(tool="plan_outing", ok=False, error=plan.rationale)

    try:
        zone = ZoneInfo(ctx.timezone)
    except Exception:  # noqa: BLE001 - an unknown zone must not break a reply
        zone = UTC

    items = []
    for position, stop in enumerate(plan.stops, start=1):
        arrive = stop.arrive_at.astimezone(zone)
        depart = stop.depart_at.astimezone(zone)
        items.append(
            {
                "id": str(stop.experience.id),
                "title": stop.experience.title,
                "summary": stop.experience.summary,
                "type": stop.experience.type,
                "category": stop.experience.category.name if stop.experience.category else None,
                "venueName": stop.experience.venue.name if stop.experience.venue else None,
                # Pre-rendered in the city's clock so the model never formats or
                # converts a time itself.
                "when": f"{arrive:%H:%M} - {depart:%H:%M}",
                "price": "Free" if stop.estimated_cost == 0 else f"{stop.estimated_cost:.0f} ETB",
                "reason": (
                    f"Stop {position}"
                    + (" - starts at a set time" if stop.is_fixed_time else "")
                    + (
                        f", {stop.travel_minutes} min from the last stop"
                        if position > 1
                        else ""
                    )
                ),
            }
        )

    return ToolResult(
        tool="plan_outing",
        ok=True,
        items=items,
        entity_ids=[str(stop.experience.id) for stop in plan.stops],
        # The plan in full, so the client can render it as a sequence and offer
        # to keep it. Carries the request that produced it too: accepting is
        # saving *this* plan, and re-deriving the parameters from prose later
        # would be guessing at what was asked.
        payload={
            "stops": [
                {
                    "experienceId": str(stop.experience.id),
                    "eventInstanceId": (
                        str(stop.event_instance_id) if stop.event_instance_id else None
                    ),
                    "title": stop.experience.title,
                    "arriveAt": stop.arrive_at.isoformat(),
                    "departAt": stop.depart_at.isoformat(),
                    "dwellMinutes": stop.dwell_minutes,
                    "travelMinutes": stop.travel_minutes,
                    "travelKm": stop.travel_km,
                    "estimatedCost": stop.estimated_cost,
                    "isFixedTime": stop.is_fixed_time,
                    "note": stop.note,
                }
                for stop in plan.stops
            ],
            "totalCost": plan.total_cost,
            "currency": "ETB",
            "totalTravelMinutes": plan.total_travel_minutes,
            "rationale": plan.rationale,
            "unmet": plan.unmet,
            "request": {
                "startsAt": request.start.isoformat(),
                "endsAt": request.end.isoformat(),
                "city": request.city_slug,
                "latitude": request.latitude,
                "longitude": request.longitude,
                "budget": request.budget,
                "maxStops": request.max_stops,
                "categories": request.categories,
                "freeOnly": request.free_only,
                # Carried forward so the next refinement inherits what the last
                # one rejected. Without it, "not that one" is forgotten as soon
                # as the explorer asks for anything else.
                "avoid": [str(i) for i in request.avoid_experience_ids],
            },
        },
    )


def _as_uuids(values: list[str] | None) -> list[uuid.UUID]:
    """Parse ids, dropping anything malformed rather than failing the turn."""
    parsed: list[uuid.UUID] = []
    for value in values or []:
        try:
            parsed.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return parsed


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


register(
    ToolDefinition(
        name="plan_outing",
        description=(
            "Build an ordered, timed itinerary for a window of time, allowing for "
            "travel between stops and treating scheduled events as fixed points."
        ),
        side_effect=SIDE_EFFECT_NONE,
        risk=RISK_LOW,
        requires_confirmation=False,
        parameters={
            "type": "object",
            "properties": {
                "starts_after": {"type": "string", "description": "ISO 8601 window start."},
                "starts_before": {"type": "string", "description": "ISO 8601 window end."},
                "budget": {"type": "number", "minimum": 0},
                "max_stops": {"type": "integer", "minimum": 1, "maximum": 6},
                "categories": {"type": "array", "items": {"type": "string"}},
                "free_only": {"type": "boolean"},
            },
        },
        handler=_plan_outing,
    )
)
