"""AI Concierge endpoints (spec 55.09).

Spec 57.04 s4 insists the concierge "is not a separate destination" - it is a layer
available anywhere. The API reflects that: a session can be created implicitly by
sending a message, so a client can open a conversation from any surface without a
setup round-trip.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.api.deps import AnonymousId, OptionalUser, SessionDep
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import BadRequestError
from app.domains.ai.gateway import AIGateway, resolve_city
from app.domains.catalog.schemas import CamelModel
from app.domains.discovery.service import build_context
from app.domains.explorer.service import ExplorerService

router = APIRouter(prefix="/assistant", tags=["concierge"])
settings = get_settings()


class MessageRequest(CamelModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    city: str = settings.default_city_slug
    latitude: float | None = None
    longitude: float | None = None


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


class SuggestedAction(CamelModel):
    label: str
    message: str


class ConciergeResponse(CamelModel):
    conversation_id: uuid.UUID
    message: str
    intent: str
    confidence: float
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


async def _reply(*, session, user, anonymous_id: str | None, payload: MessageRequest):
    if not payload.message.strip():
        raise BadRequestError("Message cannot be empty.", code="EMPTY_MESSAGE")

    city_slug, city_name, timezone = await resolve_city(session, payload.city)

    explorer = ExplorerService(session)
    saved_ids = await explorer.saved_experience_ids(user.id if user else None)
    preferences = user.profile.preferences if user and user.profile else {}
    ctx = build_context(
        latitude=payload.latitude,
        longitude=payload.longitude,
        preferences=preferences,
        saved_ids=saved_ids,
        timezone=timezone,
    )

    gateway = AIGateway(session)
    conversation = await gateway.get_or_create_conversation(
        conversation_id=payload.conversation_id,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        city_slug=city_slug,
    )
    reply = await gateway.handle_message(
        conversation=conversation,
        text=payload.message,
        ctx=ctx,
        city_slug=city_slug,
        city_name=city_name,
        timezone=timezone,
        preferences=preferences,
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
) -> Envelope[ConciergeResponse]:
    conversation, reply = await _reply(
        session=session, user=user, anonymous_id=anonymous_id, payload=payload
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
) -> StreamingResponse:
    conversation, reply = await _reply(
        session=session, user=user, anonymous_id=anonymous_id, payload=payload
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
