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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domains.ai import intents, refinement, tools
from app.domains.ai import memory as ai_memory
from app.domains.ai.memory import MemoryService, render_for_prompt
from app.domains.ai.models import Conversation, Message, UserMemory
from app.domains.ai.prompts import (
    CONCIERGE_PROMPT_VERSION,
    CONCIERGE_SYSTEM_PROMPT,
    build_context_notes,
)
from app.domains.ai.understanding import understand
from app.domains.catalog import localisation, locate
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.repository import Area
from app.domains.discovery.ranking import RankingContext
from app.domains.explorer.planning import PlanRequest
from app.integrations import timezones, weather
from app.integrations.ai_provider import GenerationRequest, get_provider

logger = get_logger("mado.ai.gateway")
settings = get_settings()

# How much prior conversation to replay. Spec 56.01 s15 warns against blindly
# including the whole history; six turns covers the follow-ups people actually make
# ("what about tomorrow?", "anything cheaper?") without unbounded growth.
HISTORY_TURNS = 6

MAX_RESULTS_IN_CONTEXT = 8

# How far ahead the forecast is offered when the explorer named no time. Enough
# to cover "this weekend" and "in a few days", short enough that the prompt does
# not carry a fortnight of weather nobody asked about.
DEFAULT_FORECAST_DAYS = 7


@dataclass(slots=True)
class ConversationPlace:
    """Somewhere the conversation is about, held across turns.

    Persisted on `conversation.state` so a follow-up inherits it. Without that,
    "we are going to New York next week, any music nights?" followed by "how is
    the weather there this weekend?" answers the second question about the
    explorer's own city - which is exactly what it did, and reads as the
    assistant having forgotten the only thing that mattered.

    Stored as plain values rather than an `Area`, because it goes through JSON
    and a tuple comes back as a list.
    """

    label: str
    latitude: float
    longitude: float
    timezone: str
    currency: str
    city_slug: str | None = None
    country_code: str | None = None
    kind: str | None = None
    radius_km: float | None = None
    bounding_box: tuple[float, float, float, float] | None = None
    # The country code *as a scoping key*, set only when the explorer named a
    # whole country. Distinct from `country_code`, which says which country the
    # place is in - Brooklyn is in US and is not scoped by it.
    area_country_code: str | None = None

    @property
    def area(self) -> Area:
        return Area(
            latitude=self.latitude,
            longitude=self.longitude,
            radius_km=self.radius_km,
            bounding_box=self.bounding_box,
            country_code=self.area_country_code,
            city_slug=self.city_slug,
        )

    @property
    def scope(self) -> tuple[Area, str | None, str, str, str]:
        """`(area, city_slug, label, timezone, currency)` in one go."""
        return self.area, self.city_slug, self.label, self.timezone, self.currency

    def as_state(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "currency": self.currency,
            "citySlug": self.city_slug,
            "countryCode": self.country_code,
            "kind": self.kind,
            "radiusKm": self.radius_km,
            "boundingBox": list(self.bounding_box) if self.bounding_box else None,
            "areaCountryCode": self.area_country_code,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> ConversationPlace:
        box = state.get("boundingBox")
        return cls(
            label=state.get("label") or "there",
            latitude=float(state["latitude"]),
            longitude=float(state["longitude"]),
            timezone=state.get("timezone") or "UTC",
            currency=state.get("currency") or "",
            city_slug=state.get("citySlug"),
            country_code=state.get("countryCode"),
            kind=state.get("kind"),
            radius_km=state.get("radiusKm"),
            # Back to a tuple: `Area` is a frozen slots dataclass compared by
            # value, and a list here would make two identical areas unequal.
            bounding_box=tuple(box) if box and len(box) == 4 else None,
            area_country_code=state.get("areaCountryCode"),
        )


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
    # Present when the turn produced an itinerary. Carried separately from
    # `items` because a plan is a sequence with timings and travel between stops,
    # and rendering it as a row of cards discards the part that makes it a plan.
    plan: dict[str, Any] | None = None
    # What a refinement actually changed, worked out by comparing the two plans
    # rather than taken from the model's account of its own work. An explorer
    # comparing two lists of four stops will not spot that the third one moved.
    plan_change: str | None = None


# Words that make a sentence a request rather than the name of a thing. A query
# built only from these named nothing, however many of them there are.
_ASKING = frozenset(
    {
        "a", "an", "and", "any", "anything", "are", "around", "at", "by", "can",
        "close", "do", "find", "for", "get", "give", "go", "good", "here", "i",
        "in", "is", "know", "looking", "me", "my", "near", "nearby", "of", "on",
        "or", "out", "please", "recommend", "share", "show", "some", "someone",
        "something", "somewhere", "suggest", "surprise", "tell", "the", "there",
        "thing", "things", "to", "us", "want", "what", "whats", "where", "with",
        "you", "your",
    }
)


def names_something(query: str) -> bool:
    """Whether a search query names a subject, or is just the asking.

    "traditional coffee" names something and "nothing matched it" is worth
    saying. "share something nearby" names nothing - it is a way of asking, and
    reporting that the catalogue contains no activity *called* "share something
    nearby" is answering a question nobody asked. The explorer asked for a
    recommendation and got one; the apology in front of it was noise, and read
    as the assistant not understanding plain English.

    Punctuation is stripped rather than split on, so "what's" reduces to
    "whats" and is recognised.
    """
    words = [
        "".join(character for character in word if character.isalnum())
        for word in query.lower().split()
    ]
    return any(word and word not in _ASKING for word in words)


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

    async def get_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Conversation:
        """Load a conversation belonging to the caller.

        404 rather than 403 for someone else's: confirming a conversation exists
        to whoever guesses its id is itself a disclosure.
        """
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None or conversation.deleted_at is not None:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")

        owned = (
            conversation.user_id == user_id
            if user_id is not None
            else conversation.anonymous_id is not None
            and conversation.anonymous_id == anonymous_id
        )
        if not owned:
            raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
        return conversation

    async def load_history(self, conversation_id: uuid.UUID) -> list[Message]:
        """The conversation so far, oldest first.

        Ordered by time *and* by role, because time alone does not order it. Both
        messages of a turn are added in one transaction and get the same
        ``created_at`` to the microsecond, so their relative order was a tie -
        and Postgres returned the pair inverted, every time.

        The model was therefore shown each answer before the question it
        answered, ending with a question nobody had replied to. It did the
        sensible thing with that and answered the dangling one: three cities out
        of four replied to "find me something free to do" with an apology about
        traditional coffee, the previous turn's subject, while holding five
        correct free results it had just been handed.

        A user message always precedes the assistant's within a turn, so ordering
        on that invariant is not a tiebreaker hack - and unlike a new sequence
        column it also repairs the conversations already stored.
        """
        result = await self.session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(
                Message.created_at.desc(),
                case((Message.role == "user", 1), else_=0),
            )
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
        city_slug: str | None,
        # Where the explorer is looking, which is not always a city: they may
        # have picked a street, or a whole country. The tools scope to this; the
        # city slug survives only for planning and stored plans, which need one.
        area: Area | None,
        city_name: str,
        timezone: str,
        currency: str = "",
        # Set when the explorer picked a place in the interface, which outranks a
        # destination they mentioned earlier in the conversation.
        place_label: str | None = None,
        preferences: dict | None = None,
        user_id: uuid.UUID | None = None,
        privacy: dict | None = None,
    ) -> ConciergeReply:
        started = time.perf_counter()

        history = await self.load_history(conversation.id)

        # 1. Understand. A model reads the message - intent, time, constraints and
        # anything the explorer revealed about themselves - in one structured call.
        # It decides what was *asked*, never what is true; facts still come from
        # tools, below.
        # The plan currently on the table, if any. Passed into comprehension so a
        # short follow-up ("make it cheaper") can be read as an adjustment to it
        # rather than as a fresh, contextless request.
        pending_plan = (conversation.state or {}).get("pendingPlan")

        reading = await understand(
            text,
            now=ctx.now,
            timezone=timezone,
            city_name=city_name,
            history=[{"role": m.role, "content": m.content} for m in history],
            pending_plan=pending_plan,
        )
        classification = reading.as_classification()

        # 1b. Work out where the question is about, which is not always where the
        # explorer is standing.
        #
        # Before this, the area came only from the client - the device's position
        # or the place picker - so a message could not move it. "Next week we are
        # going to New York, is there any music night there" was answered with
        # Azmari Night in Kazanchis, and the substitution guard then said so out
        # loud: honest about the failure, and still a failure, because the
        # question was perfectly answerable.
        place = await self._locate(
            reading=reading,
            conversation=conversation,
            ctx=ctx,
            chosen_label=place_label,
        )
        if place is not None:
            area, city_slug, city_name, timezone, currency = place.scope
            ctx = replace(
                ctx,
                # The destination's coordinates, not the explorer's. Weather,
                # proximity ordering and "what is near" all read these, and all
                # three are about the place being asked about.
                latitude=place.latitude,
                longitude=place.longitude,
                # ...but they are not standing there, so the reply must not offer
                # distances "from you". Without this the assistant cheerfully
                # told somebody in Addis that a bar was 1.6 km away from them.
                located_remotely=True,
                timezone=timezone,
                currency=currency or ctx.currency,
            )

        # 2. Recall before retrieval. What we know about this explorer can change
        # which results are worth fetching, so it has to be available to the tools
        # and not only to the phrasing step.
        memories: list[UserMemory] = []
        if user_id is not None:
            memory_service = MemoryService(self.session)
            memories = await memory_service.recall(user_id, text, privacy=privacy)

        # 2b. Fold this turn's reading and what we remember into the context the
        # tools will run against.
        #
        # It has to happen here, between comprehension and retrieval, because the
        # requirements are not knowable before the message is read and are useless
        # after the search has run. This is the step whose absence made the whole
        # feature decorative: constraints were extracted, described to the model in
        # prose, and never reached a query - so the concierge would recommend a
        # steakhouse and add that it was a shame about the vegan thing.
        ctx = await self._contextualise(ctx, reading=reading, memories=memories)

        # 3-5. Plan and execute tools. Facts are gathered before generation.
        plan_diff = None
        if classification.intent == intents.REFINE_PLAN and pending_plan:
            # Refining an existing plan substitutes nothing.
            substituted: str | None = None
            # A plan can only be refined if one was made, which needed a place.
            blocked = None
            tool_calls, results, plan, plan_diff = await self._refine_plan(
                text,
                reading=reading,
                pending=pending_plan,
                ctx=ctx,
                area=area,
            )
        else:
            tool_calls, results, plan, substituted, blocked = await self._execute_plan(
                classification, ctx=ctx, area=area
            )

        # 6. Synthesize. The model phrases; it does not decide the facts.
        reply_text, model_name = await self._synthesize(
            classification=classification,
            results=results,
            history=history,
            text=text,
            city_name=city_name,
            ctx=ctx,
            preferences=preferences,
            memories=memories,
            plan=plan,
            substituted=substituted,
            blocked=blocked,
        )

        # 7. Validate. The reader proposes its own question when it could not read
        # the message, which is almost always more useful than a generic prompt -
        # it knows what was unclear.
        clarification = None
        if blocked:
            # Nowhere to look beats every other thing that could be unclear: no
            # answer to "did you mean tonight?" helps until we know where.
            clarification = blocked
        elif reading.needs_clarification and reading.clarification_question:
            clarification = reading.clarification_question
        elif reading.is_ambiguous:
            clarification = (
                "I want to get this right - are you after somewhere to eat, something "
                "to watch, or something to do outdoors?"
            )
        elif reading.time_window is not None and reading.time_window.confidence < 0.7:
            clarification = (
                f"Just to check - did you mean {reading.time_window.label} this coming week?"
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

        # Write after replying, not before: a preference stated in this message
        # should shape the *next* answer. Applying it to the current one would make
        # the concierge appear to have known something before it was told.
        if user_id is not None and reading.preferences:
            await MemoryService(self.session).remember_revealed(
                user_id, reading.preferences, privacy=privacy
            )

        conversation.state = {
            **(conversation.state or {}),
            # The plan most recently offered, kept so "save this" stores the
            # itinerary the explorer actually saw. Recomputing on accept could
            # quietly hand back a different evening - an event may have sold out,
            # or the ranking may have shifted between the offer and the answer.
            "pendingPlan": plan,
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
            plan=plan,
            plan_change=plan_diff.describe() if plan_diff is not None else None,
        )

    async def _run_tools(
        self,
        plan: list[tuple[str, dict[str, Any]]],
        *,
        ctx: RankingContext,
        area: Area | None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any] | None,
        list[tools.ToolResult],
    ]:
        """Execute a tool plan, collecting calls, items, the last payload and why
        anything was refused.

        Extracted so refinement and first-time planning run tools the same way.
        A second copy of this loop would be a second place for "a failed tool
        must not fail the turn" to stop being true.

        Safe failure means the turn survives a failed tool. It does not mean the
        reason is thrown away, which is what used to happen here: an empty result
        set reached the model with no explanation, and the model supplied the most
        plausible one - that the catalogue had nothing. So an explorer who had
        simply never granted location was told, fluently and falsely, that there
        is no traditional coffee in Mado.
        """
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        payload: dict[str, Any] | None = None
        refusals: list[tools.ToolResult] = []

        for name, raw_arguments in plan:
            arguments = {k: v for k, v in raw_arguments.items() if v is not None}
            try:
                outcome = await tools.execute_tool(
                    name, arguments, session=self.session, ctx=ctx, area=area
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
                if outcome.payload is not None:
                    payload = outcome.payload
            else:
                refusals.append(outcome)

        return tool_calls, results, payload, refusals

    async def _refine_plan(
        self,
        text: str,
        *,
        reading,
        pending: dict[str, Any],
        ctx: RankingContext,
        area: Area | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None, Any]:
        """Adjust the plan already on the table (spec AI-004).

        Rebuilds the request that produced it, applies one delta, and re-solves
        with the untouched stops pinned. The alternative - planning again from
        the new message alone - discards the constraints the explorer gave two
        turns ago and the stops they already agreed to, which is why it reads as
        a fresh roll of the dice rather than an adjustment.
        """
        stops = pending.get("stops") or []
        previous = _request_from(pending, ctx=ctx, city_slug=area.city_slug if area else None)

        read = refinement.from_model(
            reading.entities.get("refinement"), stop_count=len(stops)
        ) or refinement.read_rules(text, stop_count=len(stops))

        if read is None:
            # Understood that they were talking about the plan but not what they
            # wanted changed. Returning the plan untouched is right: asking is
            # the next step, and altering something at random would be worse
            # than admitting the message was unclear.
            logger.info("refinement_unreadable", text_length=len(text))
            return [], [], pending, None

        stop_ids = [
            uuid.UUID(stop["experienceId"])
            for stop in stops
            if _is_uuid(stop.get("experienceId"))
        ]
        request = refinement.apply(
            read,
            previous,
            stop_experience_ids=stop_ids,
            # What the plan actually costs, so "cheaper" means cheaper than this
            # evening rather than cheaper than a budget it was already under.
            current_cost=_as_float(pending.get("totalCost")),
        )

        arguments = {
            "starts_after": request.start.isoformat(),
            "starts_before": request.end.isoformat(),
            "budget": request.budget,
            "max_stops": request.max_stops,
            "categories": request.categories,
            "free_only": request.free_only,
            "keep": [str(i) for i in request.keep_experience_ids],
            "avoid": [str(i) for i in request.avoid_experience_ids],
        }
        tool_calls, results, plan, _refusals = await self._run_tools(
            [("plan_outing", arguments)], ctx=ctx, area=area
        )

        if plan is None:
            # The refinement emptied the plan - "cheaper" with nothing cheap to
            # find. Keeping the previous plan is the honest outcome: the explorer
            # still has what they had, and the reply says it could not be done.
            logger.info("refinement_produced_nothing", kind=read.kind)
            return tool_calls, results, pending, None

        diff = refinement.diff_plans(pending, plan)
        logger.info(
            "plan_refined",
            kind=read.kind,
            degraded=read.degraded,
            added=len(diff.added),
            removed=len(diff.removed),
            kept=diff.kept_count,
            cost_delta=round(diff.cost_delta, 2),
        )
        return tool_calls, results, plan, diff

    async def _locate(
        self,
        *,
        reading,
        conversation: Conversation,
        ctx: RankingContext,
        chosen_label: str | None,
    ) -> ConversationPlace | None:
        """Where this turn is about, when it is not where the explorer is.

        Precedence, and each step earns its place:

        1. **A destination named in this message.** The most explicit thing
           anybody can do is say where they mean.
        2. **A place chosen in the interface.** Someone who moved the picker has
           acted deliberately, and that should beat something they said three
           turns ago.
        3. **A destination named earlier in this conversation.** "Any music
           nights?" after "we are going to New York" is still about New York, and
           making people restate the city every message is what makes an
           assistant feel like a search box.
        4. Nothing - the caller's own area stands, which is the ordinary case.

        Returns None for 4, so the caller leaves everything as it was.
        """
        named = (reading.entities or {}).get("destination")
        remembered = (conversation.state or {}).get("place")

        if not named:
            # A deliberate pick in the interface outranks a remembered mention.
            if chosen_label:
                return None
            if not remembered:
                return None
            # Rehydrated rather than re-resolved: the same conversation asking a
            # second question about New York should not spend a second place
            # lookup, and the answer cannot have changed.
            return ConversationPlace.from_state(remembered)

        near = (
            (ctx.latitude, ctx.longitude)
            if ctx.latitude is not None and ctx.longitude is not None
            else None
        )
        # `near` biases and never restricts, which is what lets "Bole" find the
        # neighbourhood in the explorer's own city while "New York" still reaches
        # another continent.
        resolved = await locate.resolve_place(named, near=near)
        if resolved is None:
            logger.info("destination_unresolved", destination=named)
            return None

        zone = await timezones.get_provider().zone_for(
            resolved.latitude, resolved.longitude, country_code=resolved.country_code
        )
        currency = localisation.currency_for_country(resolved.country_code)

        # A city row, only if one already exists. Planning needs a slug, and
        # `city_slug` is what stored itineraries key on - but a question about
        # somewhere must never *create* a city. A city is a consequence of
        # somebody publishing there, never of somebody asking about it.
        city_slug, _ = await catalog_repo.resolve_city_slug(
            self.session, city=None, latitude=resolved.latitude, longitude=resolved.longitude
        )

        place = ConversationPlace(
            label=resolved.label,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            timezone=zone or "UTC",
            currency=currency,
            city_slug=city_slug,
            country_code=resolved.country_code,
            kind=resolved.kind,
            radius_km=resolved.area.radius_km,
            bounding_box=resolved.area.bounding_box,
            area_country_code=resolved.area.country_code,
        )

        state = dict(conversation.state or {})
        state["place"] = place.as_state()
        conversation.state = state

        logger.info(
            "destination_resolved",
            asked=named,
            resolved=place.label,
            timezone=place.timezone,
            city_slug=place.city_slug,
        )
        return place

    async def _contextualise(
        self,
        ctx: RankingContext,
        *,
        reading,
        memories: list[UserMemory],
    ) -> RankingContext:
        """Add this turn's constraints, remembered needs and the forecast.

        Requirements come from two places and both matter. What the explorer just
        said carries the group they are with today - "my father is vegan" - while
        what the platform remembers carries their own standing needs, which they
        should not have to restate every turn. They are unioned rather than
        either winning: a nut allergy on file does not stop applying because this
        message was about a play area.
        """
        constraints = reading.constraints or {}

        required = set(constraints.get("required_suitability") or [])
        required |= set(ai_memory.requirements_from(memories))
        preferred = set(constraints.get("preferred_suitability") or [])

        # A stated indoor preference is a weather-shaped request, so it is honoured
        # the same way the forecast is rather than as a suitability claim - there
        # is no slug for "somewhere warm", and the ranker already knows how to
        # prefer indoors.
        wants_indoors = bool(constraints.get("indoor_preferred"))

        window = reading.time_window
        start = (window.start if window else ctx.now).date()
        end = (window.end if window else ctx.now).date()

        # Without a stated window, show a few days rather than only today.
        #
        # "How is the weather in New York this weekend" does not always resolve
        # to a window - it names no event and often reads as GENERAL_ASSISTANCE -
        # and passing today alone made the assistant answer "the forecast doesn't
        # reach as far as the weekend". That was false: the provider had ten days
        # and nobody had asked it for them. An answer that declines is only
        # honest when the thing genuinely is not known.
        end = max(end, start + timedelta(days=DEFAULT_FORECAST_DAYS - 1))

        forecast = await weather.forecast_for(ctx.latitude, ctx.longitude, days=10)
        day = forecast.on(start) if forecast is not None else None
        # The whole window, not only its first day. "How is the weather this
        # weekend" is a question about two days, and answering it with Saturday
        # alone is answering half of it - while `weather` stays single-day
        # because ranking scores one listing against one day.
        span = forecast.between(start, end) if forecast is not None else []

        return replace(
            ctx,
            required_suitability=ctx.required_suitability | required,
            preferred_suitability=ctx.preferred_suitability | preferred,
            weather=day,
            weather_window=span,
            is_raining=ctx.is_raining or wants_indoors,
        )

    @staticmethod
    def _spans_multiple_days(window, timezone: str) -> bool:
        """Whether a window covers more than one local calendar day.

        Local, not UTC: an evening in Addis runs past midnight UTC and would look
        like two days, turning "plan my evening" into a two-day trip with one stop
        on each side of midnight.
        """
        if window is None:
            return False
        try:
            zone = ZoneInfo(timezone)
        except Exception:  # noqa: BLE001 - an unknown zone must not break routing
            return False
        return window.start.astimezone(zone).date() != window.end.astimezone(zone).date()

    async def _execute_plan(
        self,
        classification: intents.Classification,
        *,
        ctx: RankingContext,
        area: Area | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None, bool]:
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

        # What was actually searched for, as opposed to the sentence it was
        # lifted out of. Everything the reply says about a failed match talks
        # about this - quoting somebody's whole request back at them as though
        # it were a listing title is how this went wrong.
        searched = str(classification.entities.get("query", "") or "")

        # Planning is checked first, and ahead of the time-window rule below.
        # Someone asking to plan an evening has asked for a *sequence* - answering
        # with an unordered list of things happening tonight leaves them to work out
        # the order, the travel and whether it is even possible, which is the entire
        # job they delegated.
        if classification.intent == intents.PLAN_ACTIVITY:
            # One outing or a whole stay, decided by how many days the window
            # covers rather than by a separate intent the model has to get right.
            # A span is already extracted reliably, and asking comprehension to
            # also label the difference would be a second chance to disagree with
            # itself about the same sentence.
            if self._spans_multiple_days(window, ctx.timezone):
                plan.append(
                    (
                        "plan_trip",
                        {
                            "starts_after": window.start.isoformat(),
                            "starts_before": window.end.isoformat(),
                            "categories": categories,
                            "free_only": constraints.get("free_only", False),
                            "budget": constraints.get("budget_amount"),
                            "stops_per_day": constraints.get("max_stops") or 3,
                        },
                    )
                )
            else:
                plan.append(
                    (
                        "plan_outing",
                        {
                            "starts_after": window.start.isoformat() if window else None,
                            "starts_before": window.end.isoformat() if window else None,
                            "categories": categories,
                            "free_only": constraints.get("free_only", False),
                            "budget": constraints.get("budget_amount"),
                        },
                    )
                )
        # A stated time window is decisive, whatever the intent label. "What should
        # I do tonight?" classifies as RECOMMEND_ACTIVITY on its phrasing, but the
        # explorer plainly wants things happening tonight - answering it with an
        # all-day cafe would be wrong. Spec 56.02 s24 treats temporal expressions as
        # hard constraints, so they are checked before intent routing.
        elif window is not None or classification.intent in {
            intents.DISCOVER_EVENTS,
            intents.SEARCH_EVENTS,
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
                        "query": searched,
                        "categories": categories,
                        "free_only": constraints.get("free_only", False),
                    },
                )
            )

        tool_calls, results, payload, refusals = await self._run_tools(
            plan, ctx=ctx, area=area
        )

        # De-duplicate across tools while preserving rank order.
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in results:
            identifier = item.get("id")
            if identifier in seen:
                continue
            seen.add(identifier)
            deduped.append(item)

        # Only the fallback below counts as a substitution. Low confidence in
        # the *intent* is a different thing: the search may have matched
        # perfectly while the classifier was unsure what kind of answer was
        # wanted, and saying "nothing matched" there would be a fresh
        # dishonesty rather than a cure for one. That case already has its own
        # honest signal - the concierge asks what was meant.
        #
        # Carries the words that matched nothing rather than a flag, so the
        # reply can name them. It used to quote the explorer's whole message,
        # which is how "share something nearby" came back as an activity the
        # catalogue supposedly does not have one called.
        substituted: str | None = None

        # Nothing ran, because there is nowhere to run it. That is not an empty
        # catalogue and must not be phrased as one - and there is no point
        # attempting the widening fallback below, which would be refused for the
        # same reason.
        blocked = next(
            (r.error for r in refusals if r.code == tools.NO_AREA and r.error),
            None,
        )
        if blocked and not deduped:
            return tool_calls, [], payload, False, blocked

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
                area=area,
            )
            if fallback.ok:
                deduped = fallback.items
                # Recorded, because the reply must not present these as matches.
                # Saying "here is what I found" over a list that answers nothing
                # the explorer asked is how a broken search reads as a thin
                # catalogue - which is exactly how a real one hid for a while.
                #
                # But only when the explorer named something. "Share something
                # nearby" names nothing: no listing is called that, and saying
                # so is not a fact about the catalogue. It produced "I couldn't
                # find an activity specifically called 'share something nearby'"
                # in front of a perfectly good list of nearby things - they
                # asked for a recommendation and got one, and the apology read
                # as the assistant not following plain English.
                substituted = searched if names_something(searched) else None
                tool_calls.append(
                    {
                        "tool": "search_experiences",
                        "arguments": {"fallback": True},
                        "ok": True,
                        "resultCount": len(fallback.items),
                    }
                )

        return tool_calls, deduped[:MAX_RESULTS_IN_CONTEXT], payload, substituted, None

    async def _synthesize(
        self,
        *,
        classification: intents.Classification,
        results: list[dict[str, Any]],
        history: list[Message],
        text: str,
        city_name: str,
        substituted: str | None = None,
        blocked: str | None = None,
        ctx: RankingContext,
        preferences: dict | None,
        memories: list[UserMemory] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        context_notes = build_context_notes(
            has_location=ctx.has_location,
            preferences=preferences,
            time_label=classification.time_window.label if classification.time_window else None,
            constraints=classification.constraints,
            weather=ctx.weather,
            weather_window=ctx.weather_window,
            required_suitability=ctx.required_suitability,
            located_remotely=ctx.located_remotely,
        )
        recalled = render_for_prompt(memories or [])
        if recalled:
            context_notes = "\n\n".join(filter(None, [context_notes, recalled]))
        system_prompt = CONCIERGE_SYSTEM_PROMPT.format(
            city_name=city_name,
            local_time=ctx.now.strftime("%A %d %B, %H:%M UTC"),
            context_notes=context_notes,
        )

        # A plan is rendered as an ordered itinerary, not as a list of options.
        # Handed the flat card list, the model read the stops as alternatives and
        # hedged - "the only option still available is Azmari Night... might not
        # be ideal if you prefer quieter places" - while a three-stop plan sat
        # underneath it. It cannot describe a sequence it was never shown one of.
        if plan and plan.get("stops"):
            block, wrapper = _render_plan_block(plan), "PLAN"
        else:
            block, wrapper = _render_results_block(results), "RESULTS"
        user_message = f"{text}\n\n<{wrapper}>\n{block}\n</{wrapper}>"

        if blocked:
            # Told as an instruction, not as a result, because there is no result:
            # the tools never ran. Without this the model sees an empty block and
            # explains it the only way it can - by asserting the catalogue is
            # empty, which is a claim no tool made.
            user_message += (
                f"\n\nNOTE: no search was run, because {blocked} Say exactly that, "
                "briefly, and ask for a place. Do not say anything about what the "
                "catalogue does or does not contain - nothing was looked at."
            )
        elif substituted:
            # The model has to be told, or it introduces a substitute as though
            # it were the answer - fluent prose over a list that matches nothing
            # asked for. The offline composer was taught this and the model was
            # not, so the honesty held only where nobody was using it.
            user_message += (
                f"\n\nNOTE: searching for {substituted!r} matched nothing. Those are "
                "the words that were searched for, not a title - say that nothing "
                "matched them, never that the catalogue has no activity 'called' "
                "them. The items above are other things on nearby, offered as an "
                "alternative. Say plainly that nothing matched before mentioning "
                "them, and do not present them as answers to what was asked.\n"
                "Answer the request in this message. Earlier subjects are context, "
                "not the question."
            )

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
                # True when nothing matched and the results below are a
                # substitute. The reply has to say so: presenting them as
                # matches is the difference between "we have little" and "your
                # question was ignored", and only one of those is true.
                "substituted": substituted,
                # Set when no tool ran at all. The offline composer needs it for
                # the same reason the model does: its empty-result sentence names
                # a city and claims nothing matched, and neither part is true
                # when the search never happened.
                "blocked": blocked,
            },
            temperature=0.4,
            # A plan reply lists several stops with times, so it needs more
            # room than a one-line recommendation.
            max_output_tokens=1200,
        )

        # The fast model for everything, including planning.
        #
        # Routing PLAN_ACTIVITY to the reasoning model predates the planning
        # engine, when the model was expected to work out the itinerary itself.
        # It no longer does: the planner computes the order, the timings and the
        # travel, and the model only phrases the result. Deliberation buys
        # nothing there, costs several seconds, and was actively harmful - the
        # reasoning model spent its whole budget thinking and returned an empty
        # reply.
        model = settings.ai_fast_model
        result = await self.provider.generate(request, model=model)
        return result.text.strip(), result.model


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _request_from(pending: dict[str, Any], *, ctx: RankingContext, city_slug: str) -> PlanRequest:
    """Rebuild the request that produced the plan being refined.

    The plan payload carries its own request precisely so this does not have to
    be re-derived from prose. Re-reading the original message would lose
    everything the explorer said in the turns since, and re-deriving from the
    refinement alone would lose everything they said before it.
    """
    stored = pending.get("request") or {}

    def _time(key: str, fallback: datetime) -> datetime:
        raw = stored.get(key)
        if not isinstance(raw, str):
            return fallback
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return fallback

    start = _time("startsAt", ctx.now)
    end = _time("endsAt", start + timedelta(hours=5))

    return PlanRequest(
        start=start,
        end=end if end > start else start + timedelta(hours=5),
        city_slug=stored.get("city") or city_slug,
        latitude=stored.get("latitude", ctx.latitude),
        longitude=stored.get("longitude", ctx.longitude),
        budget=stored.get("budget"),
        max_stops=int(stored.get("maxStops") or 3),
        categories=list(stored.get("categories") or []),
        free_only=bool(stored.get("freeOnly")),
        avoid_experience_ids=[
            uuid.UUID(i) for i in (stored.get("avoid") or []) if _is_uuid(i)
        ],
    )


def _render_plan_block(plan: dict[str, Any]) -> str:
    """Render an itinerary as a sequence, with the times already computed.

    Ordered and numbered, with travel drawn between stops, so the model has no
    room to read it as a menu. Times are pre-formatted for the same reason they
    are elsewhere: the model must never compute or convert one.
    """
    lines = [
        "This itinerary has already been worked out and is feasible as given.",
        "Present it in this order. Do not reorder it, add to it, or change a time.",
        "",
    ]
    for position, stop in enumerate(plan.get("stops") or [], start=1):
        arrive = str(stop.get("arriveAt", ""))[11:16]
        depart = str(stop.get("departAt", ""))[11:16]
        parts = [f"{position}. {stop.get('title')} | {arrive}-{depart}"]
        cost = stop.get("estimatedCost") or 0
        parts.append("free" if cost == 0 else f"{cost:.0f} ETB")
        if stop.get("isFixedTime"):
            parts.append("starts at a set time")
        if position > 1 and stop.get("travelMinutes"):
            parts.append(f"{stop['travelMinutes']} min from the previous stop")
        lines.append(" | ".join(parts))

    lines.append("")
    total = plan.get("totalCost") or 0
    lines.append(
        f"Totals: {len(plan.get('stops') or [])} stops, "
        f"{plan.get('totalTravelMinutes', 0)} minutes travelling, "
        + ("nothing to pay" if total == 0 else f"about {total:.0f} ETB")
    )
    for reason in plan.get("unmet") or []:
        lines.append(f"Could not fit: {reason}")
    return "\n".join(lines)


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


async def resolve_city(
    session: AsyncSession,
    slug: str | None,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[str | None, str, str, str]:
    """Return ``(slug, name, timezone, currency)`` for what this is about.

    The currency travels with the city for the same reason the timezone does:
    both are properties of the place, and a plan that renders a total has to use
    the money spent there. Quoting it from a setting produced itineraries priced
    in birr in every city Mado reached after the first.

    It used to fall back to a configured pilot city and, failing that, to a
    hardcoded Ethiopian timezone - so an explorer anywhere on earth was answered
    as though they were standing in Addis Ababa, and told so in the reply. Now
    an unresolved city is None: the tools then search unscoped rather than
    somewhere the explorer is not, and the assistant can ask.

    UTC when nothing resolves. Wrong for almost everybody, but wrong in a way
    that shifts a time rather than relocating a person.
    """
    resolved, _ = await catalog_repo.resolve_city_slug(
        session, city=slug, latitude=latitude, longitude=longitude
    )
    # No currency either when no city resolves. The empty string is deliberate:
    # the alternative is naming one, and any name would be a guess about where
    # somebody is that the rest of this function exists to stop making. A plan
    # with no city renders bare numbers rather than confidently wrong money.
    if resolved is None:
        return None, "your area", "UTC", ""

    city = await catalog_repo.get_city_by_slug(session, resolved)
    if city is None:
        return None, "your area", "UTC", ""
    return city.slug, city.name, city.timezone, city.currency
