"""AI Gateway - the single entry point for every AI request (spec 82.02 s6).

The execution pipeline follows spec 56.01 s6 exactly::

    normalize -> classify -> assemble context -> plan -> execute tools
              -> synthesize -> validate -> respond

The ordering is the whole point. Tools run *before* generation, so the model is
handed verified platform data and asked only to phrase it. That is what spec
56.01 s3.1 means by "tool-first factuality", and it is why a provider outage
degrades the concierge's fluency rather than its correctness.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domains.ai import intents, tools
from app.domains.ai.models import Conversation, Message
from app.domains.ai.prompts import (
    CONCIERGE_PROMPT_VERSION,
    CONCIERGE_SYSTEM_PROMPT,
    build_context_notes,
)
from app.domains.catalog import repository as catalog_repo
from app.domains.discovery.ranking import RankingContext
from app.integrations.ai_provider import GenerationRequest, get_provider

logger = get_logger("mado.ai.gateway")
settings = get_settings()

# How much prior conversation to replay. Spec 56.01 s15 warns against blindly
# including the whole history; six turns covers the follow-ups people actually make
# ("what about tomorrow?", "anything cheaper?") without unbounded growth.
HISTORY_TURNS = 6

MAX_RESULTS_IN_CONTEXT = 8


@dataclass(slots=True)
class ConciergeReply:
    message: str
    intent: str
    confidence: float
    items: list[dict[str, Any]] = field(default_factory=list)
    suggested_actions: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    # Set when confidence is low enough that the concierge should confirm rather
    # than assume (spec 56.02 s13 / s33).
    clarification: str | None = None


class AIGateway:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.provider = get_provider()

    # ----------------------------------------------------------- conversations

    async def get_or_create_conversation(
        self,
        *,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
        city_slug: str,
    ) -> Conversation:
        if conversation_id is not None:
            conversation = await self.session.get(Conversation, conversation_id)
            if conversation is None or conversation.deleted_at is not None:
                raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
            # An anonymous thread becomes owned once the explorer registers,
            # which is how guest-mode history survives sign-up.
            if user_id is not None and conversation.user_id is None:
                conversation.user_id = user_id
            return conversation

        conversation = Conversation(
            user_id=user_id, anonymous_id=anonymous_id, city_slug=city_slug, state={}
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def load_history(self, conversation_id: uuid.UUID) -> list[Message]:
        result = await self.session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_TURNS * 2)
        )
        return list(reversed(result.scalars().all()))

    async def list_conversations(
        self, *, user_id: uuid.UUID | None, anonymous_id: str | None, limit: int = 20
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .options(selectinload(Conversation.messages))
        )
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        elif anonymous_id:
            stmt = stmt.where(Conversation.anonymous_id == anonymous_id)
        else:
            return []
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    # -------------------------------------------------------------- the pipeline

    async def handle_message(
        self,
        *,
        conversation: Conversation,
        text: str,
        ctx: RankingContext,
        city_slug: str,
        city_name: str,
        timezone: str,
        preferences: dict | None = None,
    ) -> ConciergeReply:
        started = time.perf_counter()

        # 1-2. Normalize and classify.
        classification = intents.classify(text, now=ctx.now, timezone=timezone)

        # 3-5. Plan and execute tools. Facts are gathered before generation.
        tool_calls, results = await self._execute_plan(classification, ctx=ctx, city_slug=city_slug)

        # 6. Synthesize. The model phrases; it does not decide the facts.
        history = await self.load_history(conversation.id)
        reply_text, model_name = await self._synthesize(
            classification=classification,
            results=results,
            history=history,
            text=text,
            city_name=city_name,
            ctx=ctx,
            preferences=preferences,
        )

        # 7. Validate.
        clarification = None
        if classification.confidence < intents.CONFIDENCE_LOW:
            clarification = (
                "I want to get this right - are you after somewhere to eat, something "
                "to watch, or something to do outdoors?"
            )
        elif classification.time_window is not None and classification.time_window.confidence < 0.7:
            clarification = (
                f"Just to check - did you mean {classification.time_window.label} this coming week?"
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        entity_ids = [item["id"] for item in results if "id" in item]

        self.session.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=text,
                intent=classification.intent,
                intent_confidence=classification.confidence,
            )
        )
        self.session.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=reply_text,
                intent=classification.intent,
                intent_confidence=classification.confidence,
                model=model_name,
                prompt_version=CONCIERGE_PROMPT_VERSION,
                tool_calls=tool_calls,
                referenced_entities=entity_ids,
                latency_ms=latency_ms,
            )
        )

        conversation.state = {
            **(conversation.state or {}),
            "lastIntent": classification.intent,
            "lastTimeLabel": classification.time_window.label
            if classification.time_window
            else None,
            "lastConstraints": classification.constraints,
            "updatedAt": datetime.now(UTC).isoformat(),
        }
        if conversation.title is None:
            conversation.title = text[:80]

        logger.info(
            "concierge_reply",
            intent=classification.intent,
            confidence=classification.confidence,
            tools=[call["tool"] for call in tool_calls],
            results=len(results),
            latency_ms=latency_ms,
            model=model_name,
        )

        return ConciergeReply(
            message=reply_text,
            intent=classification.intent,
            confidence=classification.confidence,
            items=results,
            suggested_actions=_suggested_actions(classification, results),
            tool_calls=tool_calls,
            model=model_name,
            latency_ms=latency_ms,
            clarification=clarification,
        )

    async def _execute_plan(
        self, classification: intents.Classification, *, ctx: RankingContext, city_slug: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Choose and run tools for the classified intent.

        Deterministic routing rather than letting the model pick: for this set of
        read-only discovery tools the mapping is unambiguous, and spec 56.01 s3.3
        prefers structured execution plans over free generation. Model-driven
        selection becomes worthwhile when the tool surface grows.
        """
        plan: list[tuple[str, dict[str, Any]]] = []
        constraints = classification.constraints
        categories = constraints.get("categories")
        window = classification.time_window

        # A stated time window is decisive, whatever the intent label. "What should
        # I do tonight?" classifies as RECOMMEND_ACTIVITY on its phrasing, but the
        # explorer plainly wants things happening tonight - answering it with an
        # all-day cafe would be wrong. Spec 56.02 s24 treats temporal expressions as
        # hard constraints, so they are checked before intent routing.
        if window is not None or classification.intent in {
            intents.DISCOVER_EVENTS,
            intents.SEARCH_EVENTS,
            intents.PLAN_ACTIVITY,
        }:
            plan.append(
                (
                    "find_events",
                    {
                        "starts_after": window.start.isoformat() if window else None,
                        "starts_before": window.end.isoformat() if window else None,
                        "categories": categories,
                        "free_only": constraints.get("free_only", False),
                    },
                )
            )
        elif constraints.get("nearby") and ctx.has_location:
            plan.append(("find_nearby", {"radius_km": 3.0}))
        elif classification.intent in {
            intents.SEARCH_EXPERIENCES,
            intents.RECOMMEND_ACTIVITY,
            intents.GENERAL_ASSISTANCE,
            intents.COMPARE_EVENTS,
            intents.ASK_ABOUT_VENUE,
            intents.GET_EVENT_DETAILS,
        }:
            plan.append(
                (
                    "search_experiences",
                    {
                        "query": classification.entities.get("query", ""),
                        "categories": categories,
                        "free_only": constraints.get("free_only", False),
                    },
                )
            )

        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for name, raw_arguments in plan:
            arguments = {k: v for k, v in raw_arguments.items() if v is not None}
            try:
                outcome = await tools.execute_tool(
                    name, arguments, session=self.session, ctx=ctx, city_slug=city_slug
                )
            except Exception as exc:  # noqa: BLE001
                # A failed tool must not fail the turn: the concierge answers with
                # what it has (spec 56.01 s3.8 "safe failure").
                logger.warning("tool_execution_failed", tool=name, error=str(exc))
                tool_calls.append({"tool": name, "arguments": arguments, "ok": False})
                continue

            tool_calls.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "ok": outcome.ok,
                    "resultCount": len(outcome.items),
                    "freshness": outcome.freshness,
                }
            )
            if outcome.ok:
                results.extend(outcome.items)

        # De-duplicate across tools while preserving rank order.
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in results:
            identifier = item.get("id")
            if identifier in seen:
                continue
            seen.add(identifier)
            deduped.append(item)

        # A fallback so an explorer is never met with nothing at all - but *only*
        # when no time window was asked for. If someone asks what is on tonight and
        # nothing is, substituting undated places would answer a question they did
        # not ask while the reply still says "here is what is on tonight". Better to
        # return nothing and let the response layer say so and offer to widen.
        if not deduped and classification.requires_tools and window is None:
            fallback = await tools.execute_tool(
                "search_experiences",
                {"query": "", "limit": 6},
                session=self.session,
                ctx=ctx,
                city_slug=city_slug,
            )
            if fallback.ok:
                deduped = fallback.items
                tool_calls.append(
                    {
                        "tool": "search_experiences",
                        "arguments": {"fallback": True},
                        "ok": True,
                        "resultCount": len(fallback.items),
                    }
                )

        return tool_calls, deduped[:MAX_RESULTS_IN_CONTEXT]

    async def _synthesize(
        self,
        *,
        classification: intents.Classification,
        results: list[dict[str, Any]],
        history: list[Message],
        text: str,
        city_name: str,
        ctx: RankingContext,
        preferences: dict | None,
    ) -> tuple[str, str]:
        context_notes = build_context_notes(
            has_location=ctx.has_location,
            preferences=preferences,
            time_label=classification.time_window.label if classification.time_window else None,
            constraints=classification.constraints,
        )
        system_prompt = CONCIERGE_SYSTEM_PROMPT.format(
            city_name=city_name,
            local_time=ctx.now.strftime("%A %d %B, %H:%M UTC"),
            context_notes=context_notes,
        )

        results_block = _render_results_block(results)
        user_message = f"{text}\n\n<RESULTS>\n{results_block}\n</RESULTS>"

        request = GenerationRequest(
            system_prompt=system_prompt,
            user_message=user_message,
            history=[{"role": m.role, "content": m.content} for m in history],
            context={
                "intent": classification.intent,
                "results": results,
                "city_name": city_name,
                "time_label": classification.time_window.label
                if classification.time_window
                else None,
            },
            temperature=0.4,
        )

        model = (
            settings.ai_reasoning_model
            if classification.intent == intents.PLAN_ACTIVITY
            else settings.ai_fast_model
        )
        result = await self.provider.generate(request, model=model)
        return result.text.strip(), result.model


def _render_results_block(results: list[dict[str, Any]]) -> str:
    """Render tool results as compact lines for the prompt.

    Compact on purpose - every token here is context budget (spec 56.01 s15) - and
    labelled so the model can attribute each fact to a specific retrieved record.
    """
    if not results:
        return "(no matching experiences were found)"

    lines = []
    for index, item in enumerate(results, start=1):
        parts = [f"{index}. {item.get('title')}"]
        if item.get("category"):
            parts.append(f"category={item['category']}")
        if item.get("venue_name"):
            parts.append(f"venue={item['venue_name']}")
        if item.get("neighborhood"):
            parts.append(f"area={item['neighborhood']}")
        if item.get("when"):
            parts.append(f"when={item['when']}")
        if item.get("price"):
            parts.append(f"price={item['price']}")
        if item.get("distance_km") is not None:
            parts.append(f"distance={item['distance_km']}km")
        if item.get("rating") is not None:
            parts.append(f"rating={item['rating']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _suggested_actions(
    classification: intents.Classification, results: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Follow-ups that turn an answer into a next step (spec 57.01 s32)."""
    actions: list[dict[str, str]] = []
    if not results:
        # An empty time-bounded answer needs a way out of the dead end, and the
        # useful move is widening the window rather than repeating the question.
        if classification.time_window is not None:
            actions.append({"label": "Try this week", "message": "What is on this week?"})
            actions.append(
                {"label": "Places open anyway", "message": "Show me places I can go right now"}
            )
        else:
            actions.append({"label": "What's on today?", "message": "What's on today?"})
            actions.append({"label": "Something free", "message": "Find me something free to do"})
        return actions

    if classification.intent in {intents.DISCOVER_EVENTS, intents.SEARCH_EVENTS}:
        actions.append({"label": "Only free ones", "message": "Show me just the free ones"})
        actions.append({"label": "Closer to me", "message": "Which of these is closest to me?"})
    else:
        actions.append({"label": "What about tonight?", "message": "What about tonight?"})
        actions.append({"label": "Something cheaper", "message": "Anything cheaper?"})

    actions.append({"label": "Plan my evening", "message": "Plan my evening around one of these"})
    return actions


async def resolve_city(session: AsyncSession, slug: str) -> tuple[str, str, str]:
    """Return ``(slug, name, timezone)``, falling back to the configured pilot city."""
    city = await catalog_repo.get_city_by_slug(session, slug)
    if city is None:
        city = await catalog_repo.get_city_by_slug(session, settings.default_city_slug)
    if city is None:
        return settings.default_city_slug, "the city", "Africa/Addis_Ababa"
    return city.slug, city.name, city.timezone
