"""AI Concierge endpoints (spec 55.09).

Spec 57.04 s4 insists the concierge "is not a separate destination" - it is a layer
available anywhere. The API reflects that: a session can be created implicitly by
sending a message, so a client can open a conversation from any surface without a
setup round-trip.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.api.deps import AnonymousId, CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.domains.ai.gateway import AIGateway, resolve_city
from app.domains.ai.memory import MemoryService, effective_confidence, memory_enabled
from app.domains.catalog.repository import Area
from app.domains.catalog.schemas import CamelModel
from app.domains.discovery.service import build_context
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.planning_service import PlanningService
from app.domains.explorer.service import ExplorerService
from app.domains.explorer.social import SocialService

router = APIRouter(prefix="/assistant", tags=["concierge"])

# How far around the explorer the concierge looks when they have shared a
# location and chosen nothing. Wider than the discovery feed's first step: a
# conversation asks one question and wants an answer, where a feed can widen
# quietly between rails.
NEARBY_RADIUS_KM = 15.0
logger = get_logger("mado.concierge")
settings = get_settings()


class MessageRequest(CamelModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Where the explorer is looking, when that is not simply where they are.
    # Without these the concierge answers about the city they are standing in,
    # and tells somebody who selected Kenya that it only knows Addis Ababa.
    radius_km: float | None = Field(default=None, ge=0.2, le=200)
    bbox: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    place_label: str | None = Field(default=None, max_length=160)
    # The active draft the explorer is working on in the builder, if any.
    # Passed so the gateway can scope refinements (make it cheaper, add lunch,
    # optimize the route) to that document without asking again.
    active_itinerary_id: uuid.UUID | None = None


class ResultItem(CamelModel):
    id: str
    title: str
    summary: str | None = None
    type: str | None = None
    category: str | None = None
    venue_name: str | None = None
    neighborhood: str | None = None
    when: str | None = None
    price: str | None = None
    reason: str | None = None
    distance_km: float | None = None
    rating: float | None = None
    # A result in the chat is a post like any other, so it can be reposted from
    # there. Defaulted rather than required because the plan tools build their
    # items by hand and have no count to give.
    repost_count: int = 0
    is_reposted: bool = False
    publisher_name: str | None = None
    publisher_slug: str | None = None
    publisher_type: str | None = None


class SuggestedAction(CamelModel):
    label: str
    message: str


class PlanStopOut(CamelModel):
    experience_id: str
    event_instance_id: str | None = None
    title: str
    arrive_at: datetime
    depart_at: datetime
    dwell_minutes: int
    travel_minutes: int
    travel_km: float | None = None
    estimated_cost: float
    is_fixed_time: bool
    note: str | None = None
    day_index: int = 0


class OfferedPlan(CamelModel):
    """An itinerary the concierge worked out, offered for the explorer to keep.

    Sent separately from `results` because a plan is a sequence - the order, the
    timings and the travel between stops are the substance of it, and a row of
    cards shows none of that. Multi-day trips are flattened into ``stops`` with
    ``dayIndex`` so the same card and open-in-planner path work for both.
    """

    stops: list[PlanStopOut]
    total_cost: float
    currency: str = "ETB"
    total_travel_minutes: int
    rationale: str
    unmet: list[str] = Field(default_factory=list)
    kind: str = "outing"


class ConciergeResponse(CamelModel):
    conversation_id: uuid.UUID
    message: str
    intent: str
    confidence: float
    # Present when this turn produced an itinerary. Nothing is stored until the
    # explorer accepts it - most plans are looked at once and asked again.
    plan: OfferedPlan | None = None
    # One sentence naming what a refinement changed. Absent on a first plan,
    # because there is nothing to have changed.
    plan_change: str | None = None
    # Rendered as result cards, not prose (spec 57.04 s10).
    results: list[ResultItem] = Field(default_factory=list)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    clarification: str | None = None
    model: str | None = None
    latency_ms: int | None = None


class MessageOut(CamelModel):
    id: uuid.UUID
    role: str
    content: str
    intent: str | None = None
    created_at: datetime


class ConversationOut(CamelModel):
    id: uuid.UUID
    title: str | None = None
    city_slug: str | None = None
    updated_at: datetime


async def _reply(*, session, user, anonymous_id: str | None, payload: MessageRequest, request):
    # Each turn costs a comprehension call plus a generation call.
    await rate_limit.check(
        rate_limit.identify(request, str(user.id) if user else None),
        rate_limit.CONCIERGE_LIMIT,
    )
    if not payload.message.strip():
        raise BadRequestError("Message cannot be empty.", code="EMPTY_MESSAGE")

    city_slug, city_name, timezone, currency = await resolve_city(
        session,
        payload.city,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    # The area the tools search. A chosen place wins over the explorer's own
    # position, and may be a whole country - which a radius cannot express, so a
    # bounding box comes with it when the place has real extent.
    box: tuple[float, float, float, float] | None = None
    if payload.bbox:
        try:
            south, west, north, east = (float(part) for part in payload.bbox.split(","))
            box = (south, west, north, east) if south <= north and west <= east else None
        except (TypeError, ValueError):
            box = None

    if payload.country:
        area = Area(
            latitude=payload.latitude,
            longitude=payload.longitude,
            country_code=payload.country,
        )
    elif payload.latitude is not None and payload.longitude is not None:
        area = Area(
            latitude=payload.latitude,
            longitude=payload.longitude,
            radius_km=payload.radius_km or (None if box else NEARBY_RADIUS_KM),
            bounding_box=box,
            city_slug=city_slug,
        )
    else:
        area = Area(city_slug=city_slug) if city_slug else None

    # What the reply calls where it is looking. A chosen place names itself; a
    # resolved city names the city. Saying "Addis Ababa" to somebody who asked
    # about Kenya is the bug this replaces.
    if payload.place_label:
        city_name = payload.place_label

    explorer = ExplorerService(session)
    saved_ids = await explorer.saved_experience_ids(user.id if user else None)
    # Fetched up front, like saves, because the concierge only decides what to
    # show once its tools have run - there is nothing to ask about until it is
    # too late to ask. Empty for an anonymous explorer, which reads correctly as
    # "not marked".
    reposted_ids = await SocialService(session).all_reposted_ids(
        user.id if user else None
    )
    preferences = user.profile.preferences if user and user.profile else {}
    # None rather than {} for anonymous explorers: there is no consent on file for
    # someone we cannot identify, and memory treats "unknown" as "no".
    privacy = user.profile.privacy if user and user.profile else None
    inferred = (
        await infer_preferences(session, user_id=user.id, privacy=privacy) if user else None
    )
    ctx = build_context(
        latitude=payload.latitude,
        longitude=payload.longitude,
        preferences=preferences,
        saved_ids=saved_ids,
        reposted_ids=reposted_ids,
        timezone=timezone,
        inferred=inferred,
    )
    # The forecast and this turn's stated requirements are added inside the
    # gateway, which is the only place that has read the message yet.
    ctx = replace(ctx, currency=currency or ctx.currency)

    gateway = AIGateway(session)
    conversation = await gateway.get_or_create_conversation(
        conversation_id=payload.conversation_id,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        city_slug=city_slug,
    )

    # Record the active builder draft so the gateway can scope refinements
    # ("make it cheaper", "add lunch", "optimize") to that document. Stored in
    # conversation state rather than passed as a parameter so it survives into
    # subsequent turns without the client having to repeat it.
    if payload.active_itinerary_id is not None:
        state = dict(conversation.state or {})
        state["activeDraftId"] = str(payload.active_itinerary_id)
        conversation.state = state

    reply = await gateway.handle_message(
        conversation=conversation,
        text=payload.message,
        ctx=ctx,
        city_slug=city_slug,
        area=area,
        city_name=city_name,
        timezone=timezone,
        currency=currency,
        # Only when they actually picked one. `city_name` is set from it above,
        # so passing that instead would make every turn look like a deliberate
        # choice and permanently outrank a destination mentioned in conversation.
        place_label=payload.place_label,
        preferences=preferences,
        user_id=user.id if user else None,
        privacy=privacy,
    )
    await session.commit()
    return conversation, reply


@router.post(
    "/messages",
    response_model=Envelope[ConciergeResponse],
    summary="Send a message to the concierge",
    description=(
        "Creates a conversation implicitly when conversationId is omitted. Tools run "
        "before generation, so every fact in the reply comes from platform data."
    ),
)
async def send_message(
    payload: MessageRequest,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    request: Request,
) -> Envelope[ConciergeResponse]:
    conversation, reply = await _reply(
        session=session, user=user, anonymous_id=anonymous_id, payload=payload, request=request
    )
    return Envelope(
        data=ConciergeResponse(
            conversation_id=conversation.id,
            message=reply.message,
            intent=reply.intent,
            confidence=reply.confidence,
            results=[ResultItem(**item) for item in reply.items],
            suggested_actions=[SuggestedAction(**a) for a in reply.suggested_actions],
            clarification=reply.clarification,
            model=reply.model,
            latency_ms=reply.latency_ms,
            plan=_to_offered_plan(reply.plan),
            plan_change=reply.plan_change,
        )
    )


@router.post(
    "/messages/stream",
    summary="Send a message and stream the reply",
    description=(
        "Server-sent events. Result cards are emitted first so the interface can "
        "render them while the prose is still arriving (spec 57.01 s36)."
    ),
)
async def stream_message(
    payload: MessageRequest,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    request: Request,
) -> StreamingResponse:
    conversation, reply = await _reply(
        session=session, user=user, anonymous_id=anonymous_id, payload=payload, request=request
    )
    # The turn is already committed by _reply, which matters more here than
    # elsewhere: once the response body starts streaming the transaction can no
    # longer be rolled back cleanly.

    async def event_stream():
        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        yield sse(
            "start",
            {
                "conversationId": str(conversation.id),
                "intent": reply.intent,
                "confidence": reply.confidence,
            },
        )

        if reply.items:
            yield sse(
                "results",
                {"results": [ResultItem(**item).model_dump(by_alias=True) for item in reply.items]},
            )

        # Word-level chunking keeps the cadence natural without splitting words
        # across frames, which reads as glitchy.
        buffer: list[str] = []
        for word in reply.message.split(" "):
            buffer.append(word)
            if len(buffer) >= 6:
                yield sse("delta", {"text": " ".join(buffer) + " "})
                buffer = []
        if buffer:
            yield sse("delta", {"text": " ".join(buffer)})

        if reply.clarification:
            yield sse("clarification", {"text": reply.clarification})

        yield sse(
            "done",
            {
                "suggestedActions": [
                    SuggestedAction(**a).model_dump(by_alias=True) for a in reply.suggested_actions
                ],
                "model": reply.model,
                "latencyMs": reply.latency_ms,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/conversations",
    response_model=CollectionEnvelope[ConversationOut],
    summary="List conversations",
)
async def list_conversations(
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    limit: int = Query(default=20, ge=1, le=50),
) -> CollectionEnvelope[ConversationOut]:
    conversations = await AIGateway(session).list_conversations(
        user_id=user.id if user else None, anonymous_id=anonymous_id, limit=limit
    )
    return CollectionEnvelope(data=[ConversationOut.model_validate(c) for c in conversations])


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=CollectionEnvelope[MessageOut],
    summary="Get a conversation's messages",
)
async def conversation_messages(
    conversation_id: uuid.UUID, session: SessionDep
) -> CollectionEnvelope[MessageOut]:
    messages = await AIGateway(session).load_history(conversation_id)
    return CollectionEnvelope(data=[MessageOut.model_validate(m) for m in messages])


# --- Memory ------------------------------------------------------------------
# Spec PRODUCT-00 principle 9 makes personalization revocable, which is only true
# if the explorer can see what was remembered and remove it. A memory store with
# no inspection surface is a black box that happens to hold personal data.


class MemoryOut(CamelModel):
    id: uuid.UUID
    type: str
    category: str | None
    attribute: str | None
    value: str
    # What this memory is worth *today*, after decay - not the raw stored figure.
    # Showing the stored value would misrepresent an old memory as still strong.
    confidence: float
    source: str
    # True when the explorer stated it, false when the platform inferred it. The
    # distinction is the point: it lets someone challenge a guess we made.
    is_explicit: bool
    last_reinforced_at: datetime | None


@router.get(
    "/memory",
    response_model=CollectionEnvelope[MemoryOut],
    summary="List what the concierge remembers about you",
)
async def list_memories(
    session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[MemoryOut]:
    privacy = user.profile.privacy if user.profile else None
    if not memory_enabled(privacy):
        return CollectionEnvelope(data=[])

    memories = await MemoryService(session).list_for(user.id)
    now = datetime.now(UTC)
    return CollectionEnvelope(
        data=sorted(
            (
                MemoryOut(
                    id=m.id,
                    type=m.type,
                    category=m.category,
                    attribute=m.attribute,
                    value=m.value,
                    confidence=round(effective_confidence(m, now=now), 3),
                    source=m.source,
                    is_explicit=m.is_explicit,
                    last_reinforced_at=m.last_reinforced_at,
                )
                for m in memories
            ),
            key=lambda m: m.confidence,
            reverse=True,
        )
    )


@router.delete(
    "/memory/{memory_id}",
    status_code=204,
    summary="Forget one thing",
)
async def forget_memory(memory_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    if not await MemoryService(session).forget(user.id, memory_id):
        raise NotFoundError("Memory not found.", code="MEMORY_NOT_FOUND")
    await session.commit()


@router.delete(
    "/memory",
    status_code=204,
    summary="Forget everything",
)
async def forget_all_memories(session: SessionDep, user: CurrentUser) -> None:
    count = await MemoryService(session).forget_all(user.id)
    await session.commit()
    logger.info("memory_cleared", user_id=str(user.id), count=count)


def _to_offered_plan(plan: dict | None) -> OfferedPlan | None:
    from datetime import datetime as _dt

    from app.domains.explorer.planning_service import _flatten_offered_stops

    if not plan:
        return None
    stops = _flatten_offered_stops(plan)
    if not stops:
        return None

    def _ts(value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        return _dt.fromisoformat(str(value).replace("Z", "+00:00"))

    kind = "trip" if plan.get("days") else "outing"
    total_travel = int(plan.get("totalTravelMinutes") or 0)
    if not total_travel:
        total_travel = sum(int(s.get("travelMinutes") or 0) for s in stops)

    return OfferedPlan(
        stops=[
            PlanStopOut(
                experience_id=str(s["experienceId"]),
                event_instance_id=s.get("eventInstanceId"),
                title=s["title"],
                arrive_at=_ts(s["arriveAt"]),
                depart_at=_ts(s["departAt"]),
                dwell_minutes=int(s.get("dwellMinutes") or 0),
                travel_minutes=int(s.get("travelMinutes") or 0),
                travel_km=s.get("travelKm"),
                estimated_cost=float(s.get("estimatedCost") or 0),
                is_fixed_time=bool(s.get("isFixedTime")),
                note=s.get("note"),
                day_index=int(s.get("dayIndex") or 0),
            )
            for s in stops
        ],
        total_cost=plan.get("totalCost", 0.0),
        currency=plan.get("currency", "ETB"),
        total_travel_minutes=total_travel,
        rationale=plan.get("rationale", ""),
        unmet=plan.get("unmet") or [],
        kind=kind,
    )


class AcceptPlanRequest(CamelModel):
    title: str | None = Field(default=None, max_length=200)


@router.post(
    "/conversations/{conversation_id}/plan",
    response_model=Envelope[dict],
    status_code=201,
    summary="Keep the plan the concierge offered",
    description=(
        "Saves the itinerary from the most recent planning turn in this "
        "conversation, exactly as it was shown. Nothing is stored before this - "
        "most plans are looked at once and a different one asked for."
    ),
)
async def accept_plan(
    conversation_id: uuid.UUID,
    payload: AcceptPlanRequest,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[dict]:
    conversation = await AIGateway(session).get_conversation(
        conversation_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )

    offered = (conversation.state or {}).get("pendingPlan")
    from app.domains.explorer.planning_service import _flatten_offered_stops

    if not offered or not _flatten_offered_stops(offered):
        raise BadRequestError(
            "There is no plan in this conversation to keep.", code="NO_PENDING_PLAN"
        )

    title = (payload.title or "").strip() or _default_title(offered)
    itinerary = await PlanningService(session).save_offered(
        offered,
        title=title,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )

    # Clear it, so the same plan cannot be saved twice by a double tap.
    conversation.state = {**(conversation.state or {}), "pendingPlan": None}
    await session.commit()

    return Envelope(data={"id": str(itinerary.id), "title": itinerary.title})


@router.post(
    "/conversations/{conversation_id}/plan/draft",
    response_model=Envelope[dict],
    status_code=201,
    summary="Open the offered plan as an editable draft",
    description=(
        "Materialises the most recent plan offer as a draft itinerary so the "
        "explorer can edit stops and times in the builder before keeping it. "
        "Nothing is saved as a kept plan yet."
    ),
)
async def open_plan_as_draft(
    conversation_id: uuid.UUID,
    payload: AcceptPlanRequest,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[dict]:
    from app.domains.explorer.planning_service import STATUS_DRAFT

    conversation = await AIGateway(session).get_conversation(
        conversation_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )

    offered = (conversation.state or {}).get("pendingPlan")
    from app.domains.explorer.planning_service import _flatten_offered_stops

    if not offered or not _flatten_offered_stops(offered):
        raise BadRequestError(
            "There is no plan in this conversation to open.", code="NO_PENDING_PLAN"
        )

    title = (payload.title or "").strip() or _default_title(offered)
    draft = await PlanningService(session).save_offered(
        offered,
        title=title,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        status=STATUS_DRAFT,
    )

    # Leave the pendingPlan in state so the concierge still shows the offer
    # in the timeline — the explorer opened it in the builder, not accepted it
    # or discarded it.
    await session.commit()

    return Envelope(data={"id": str(draft.id), "title": draft.title, "status": draft.status})


# How a plan is named when the explorer does not name it. Weekday and date,
# because that is how people refer to an evening they have planned. Built from
# parts rather than a format string: the no-padding directive for day-of-month
# differs between platforms (%-d against %#d) and neither is portable.
def _default_title(offered: dict) -> str:
    from app.domains.explorer.planning_service import _flatten_offered_stops

    stops = _flatten_offered_stops(offered)
    if not stops:
        return "A plan"
    raw = stops[0]["arriveAt"]
    when = (
        raw
        if isinstance(raw, datetime)
        else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    )
    if offered.get("days"):
        return f"Trip from {when:%A} {when.day} {when:%B}"
    return f"{when:%A} {when.day} {when:%B}"


class ProactiveSuggestionOut(CamelModel):
    """One thing the concierge offers before being asked (spec AI-005)."""

    experience_id: uuid.UUID
    title: str
    summary: str | None = None
    category: str | None = None
    venue_name: str | None = None
    when: datetime | None = None
    """Why this and not something else, in the explorer's terms."""
    reason: str


@router.get(
    "/suggestion",
    response_model=Envelope[ProactiveSuggestionOut | None],
    summary="Something you might like, before you ask",
    description=(
        "Returns one thing on soon that matches what this explorer actually "
        "opens and saves, or nothing at all.\n\n"
        "Chosen by the same code that decides whether to send a nearby "
        "notification, so the concierge and the notification cannot tell "
        "somebody two different things about their own taste. What differs is "
        "permission: a notification interrupts and is capped to one a week, "
        "while this is shown in a panel the explorer just opened and so needs "
        "no cooldown.\n\n"
        "Nothing is generated. The suggestion is a real listing picked by a "
        "query, and the sentence explaining it is assembled from the same "
        "fields - a model is not asked to justify a choice it did not make "
        "(spec 56.01 s3.1)."
    ),
)
async def proactive_suggestion(
    session: SessionDep, user: CurrentUser
) -> Envelope[ProactiveSuggestionOut | None]:
    from app.domains.explorer.suggestions import for_explorer
    from app.domains.identity.service import IdentityService

    profile = await IdentityService(session).get_profile(user)
    picked = await for_explorer(session, user, profile)
    if picked is None:
        # Nothing worth saying is a valid answer, and a normal one: no stated
        # city, no affinity yet, or nothing on that clears the quality floor.
        # Padding it with the most popular thing in town would turn a
        # suggestion into an advert.
        return Envelope(data=None)

    experience, occurrence = picked
    category = experience.category.name if experience.category else None
    return Envelope(
        data=ProactiveSuggestionOut(
            experience_id=experience.id,
            title=experience.title,
            summary=experience.summary,
            category=category,
            venue_name=experience.venue.name if experience.venue else None,
            when=occurrence.start_time if occurrence else None,
            reason=(
                f"You have been opening {category.lower()} lately"
                if category
                else "Based on what you have been opening"
            ),
        )
    )
