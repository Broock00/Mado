"""Model-driven comprehension.

This is where Mado works out what an explorer meant. It replaces regular-expression
classification, which failed in the way pattern matching always fails: it could only
recognise the phrasings someone had thought to write down. "plan my evening" matched
and "sort out my night" did not. "I'm vegetarian" was caught and "I don't eat meat"
was not. Every gap was invisible until a real person hit it, and every fix was
another pattern that made the next gap harder to see.

A model reads meaning rather than surface form, so paraphrase, indirect phrasing,
mixed-language input and unusual word order all work without anyone anticipating
them.

**One call, not several.** Intent, time resolution, constraints and revealed
preferences are extracted together. Splitting them would cost several round-trips
and, worse, would let the parts disagree - a classifier calling something
PLAN_ACTIVITY while a separate extractor found no time window produces an
incoherent request that neither component can detect.

**Structured output, not prose.** The response schema is enforced by the provider,
so the result is parsed rather than interpreted. Spec 56.01 s9 prefers a schema over
parsing prose for exactly this reason.

**This does not make the model an authority on facts.** It decides what was *asked*,
never what is true. Retrieval still runs afterwards against real data, and the
generation prompt still may not invent an experience. Spec 56.01 s3.1's ordering -
tools before generation - is unchanged; understanding simply happens before both.

**Degradation.** When no provider is configured or a call fails, comprehension falls
back to the deterministic rules in :mod:`app.domains.ai.intents`. Those rules are no
longer the product's understanding of language - they are a floor that keeps the
concierge answering during an outage, and the reply is marked degraded so the
difference is visible rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.domains.ai import intents, refinement
from app.domains.ai.prompts import (
    UNDERSTANDING_PROMPT_VERSION,
    UNDERSTANDING_RESPONSE_SCHEMA,
    UNDERSTANDING_SYSTEM_PROMPT,
)
from app.domains.catalog import suitability as suitability_vocab
from app.integrations.ai_provider import GenerationRequest, get_provider

logger = get_logger("mado.ai.understanding")

# How many previous turns the reader sees. Enough for "what about tomorrow?" to
# resolve against the question before it; short enough that an old topic does not
# drag a new question back toward itself.
HISTORY_TURNS = 6

# Comprehension runs before anything else on every message, so it uses the fast
# model. Reading a sentence is not the reasoning-model task in this pipeline.
UNDERSTANDING_TEMPERATURE = 0.1


@dataclass(slots=True)
class RevealedPreference:
    """Something durable the explorer said about themselves."""

    attribute: str
    value: str
    confidence: float


@dataclass(slots=True)
class Understanding:
    """The structured reading of one message."""

    intent: str
    confidence: float
    entities: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)
    time_window: intents.TimeWindow | None = None
    preferences: list[RevealedPreference] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    requires_tools: bool = True
    # True when the rules produced this rather than the model, so the caller can
    # surface reduced comprehension instead of pretending nothing changed.
    degraded: bool = False
    prompt_version: str = UNDERSTANDING_PROMPT_VERSION

    @property
    def is_ambiguous(self) -> bool:
        return self.confidence < intents.CONFIDENCE_LOW

    def as_classification(self) -> intents.Classification:
        """Adapt to the shape the execution planner already consumes."""
        return intents.Classification(
            intent=self.intent,
            confidence=self.confidence,
            entities=self.entities,
            constraints=self.constraints,
            time_window=self.time_window,
            requires_tools=self.requires_tools,
        )


async def understand(
    text: str,
    *,
    now: datetime,
    timezone: str,
    city_name: str,
    history: list[dict[str, str]] | None = None,
    pending_plan: dict | None = None,
) -> Understanding:
    """Read one message. Never raises - comprehension must not fail a turn."""
    provider = get_provider()

    # The offline provider cannot follow a schema, so there is nothing to gain from
    # asking it. Going straight to the rules keeps local development working and
    # avoids a pointless round-trip.
    if getattr(provider, "name", "stub") == "stub":
        return _from_rules(
            text, now=now, timezone=timezone, reason="offline_provider", pending_plan=pending_plan
        )

    try:
        local_now = now.astimezone(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 - an unknown zone must not break comprehension
        local_now = now
        timezone = "UTC"

    system_prompt = UNDERSTANDING_SYSTEM_PROMPT.format(
        city_name=city_name,
        local_time=local_now.strftime("%A %d %B %Y, %H:%M"),
        timezone=timezone,
        pending_plan=_describe_pending(pending_plan),
    )

    request = GenerationRequest(
        system_prompt=system_prompt,
        user_message=text,
        history=(history or [])[-HISTORY_TURNS:],
        response_schema=UNDERSTANDING_RESPONSE_SCHEMA,
        temperature=UNDERSTANDING_TEMPERATURE,
        max_output_tokens=1200,
        # Reading a sentence needs no deliberation, and on 2.5 models the thinking
        # budget comes out of the output budget - leaving it on truncated the JSON
        # on longer messages and looked like a malformed response.
        thinking=False,
    )

    try:
        result = await provider.generate(request, model=_fast_model())
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the turn
        logger.warning("understanding_call_failed", error=str(exc))
        return _from_rules(
            text, now=now, timezone=timezone, reason="provider_error", pending_plan=pending_plan
        )

    payload = result.structured
    if not isinstance(payload, dict) or not payload.get("intent"):
        logger.warning("understanding_unparseable", model=result.model)
        return _from_rules(
            text, now=now, timezone=timezone, reason="unparseable", pending_plan=pending_plan
        )

    understanding = _from_payload(payload, now=now, timezone=timezone)
    logger.info(
        "understanding_complete",
        intent=understanding.intent,
        confidence=understanding.confidence,
        has_window=understanding.time_window is not None,
        constraints=sorted(understanding.constraints),
        preferences=len(understanding.preferences),
    )
    return understanding


def _describe_pending(plan: dict | None) -> str:
    """Show the model the plan being talked about, numbered as the explorer saw it.

    Numbered because "the last one" and "the second" are how people refer to
    stops, and a position is far more reliable than a title the model has to
    match against half-remembered prose.
    """
    stops = (plan or {}).get("stops") or []
    if not stops:
        return "No plan is pending. REFINE_PLAN is not available this turn."

    lines = [
        f"{index}. {stop.get('title', 'Untitled')}"
        + (f" - {stop.get('arriveAt', '')[11:16]}" if stop.get("arriveAt") else "")
        for index, stop in enumerate(stops)
    ]
    total = (plan or {}).get("totalCost")
    footer = f"\nTotal about {total:.0f} birr." if isinstance(total, int | float) else ""
    body = "\n".join(lines)
    return f"A plan is pending. The explorer is looking at:\n{body}{footer}"


def _fast_model() -> str:
    from app.core.config import get_settings

    return get_settings().ai_fast_model


# --- payload mapping ---------------------------------------------------------


# Camel in the schema (it faces a model and the API), snake internally.
_CONSTRAINT_KEYS = {
    "freeOnly": "free_only",
    "budgetAmount": "budget_amount",
    "nearby": "nearby",
    "familyFriendly": "family_friendly",
    "groupSize": "group_size",
    "indoorPreferred": "indoor_preferred",
    "outdoorPreferred": "outdoor_preferred",
    "accessibilityRequired": "accessibility_required",
    "maxStops": "max_stops",
    "categories": "categories",
    "requiredSuitability": "required_suitability",
    "preferredSuitability": "preferred_suitability",
}

# Constraints holding suitability slugs, which are normalised against the
# vocabulary rather than trusted. The model is told the list and mostly obeys it;
# "mostly" is not good enough for a value that becomes a database filter, and an
# unrecognised slug as a *requirement* would return an empty answer that looks
# like a city with nothing in it.
_SUITABILITY_KEYS = ("required_suitability", "preferred_suitability")

_VALID_ATTRIBUTES = frozenset(
    {"likes", "dislikes", "avoids", "restriction", "allergy", "companions", "budget"}
)

# Longest a remembered preference value may be. A model asked for something short
# occasionally returns a sentence; storing that would put a paragraph into every
# future prompt.
MAX_PREFERENCE_VALUE = 60

# Longest a destination may be. Generous enough for "Stratford-upon-Avon,
# Warwickshire" and short enough that a returned sentence is discarded rather
# than sent to a place provider as a search.
MAX_DESTINATION_LENGTH = 120


def _from_payload(payload: dict, *, now: datetime, timezone: str) -> Understanding:
    intent = payload.get("intent", intents.GENERAL_ASSISTANCE)
    confidence = _clamp(payload.get("intentConfidence"), default=0.7)

    constraints: dict = {}
    for source_key, target_key in _CONSTRAINT_KEYS.items():
        value = (payload.get("constraints") or {}).get(source_key)
        # Falsy-but-meaningful values are dropped on purpose: `freeOnly: false`
        # carries no information the absence of the key does not, and keeping it
        # would make every constraint dict look populated.
        if value in (None, False, [], ""):
            continue
        constraints[target_key] = value

    for key in _SUITABILITY_KEYS:
        if key in constraints:
            cleaned = suitability_vocab.normalise(constraints[key])
            if cleaned:
                constraints[key] = cleaned
            else:
                # Everything the model returned was outside the vocabulary. Drop
                # the key entirely rather than leaving an empty list, so nothing
                # downstream reads "they asked for nothing" as "they asked".
                constraints.pop(key)

    # An accessibility need stated as a boolean is the same need stated as a
    # slug, and the retrieval can only act on the slug. Folding it here means the
    # older constraint keeps working and stops being decorative - it was reaching
    # the prompt as a sentence and the database not at all.
    if constraints.get("accessibility_required"):
        required = set(constraints.get("required_suitability") or [])
        required.add(suitability_vocab.STEP_FREE_ACCESS)
        constraints["required_suitability"] = sorted(required)

    entities: dict = {}
    query = (payload.get("searchQuery") or "").strip()
    if query:
        entities["query"] = query

    # Somewhere the explorer named that is not where they are standing. Carried
    # as free text and resolved by the gateway through the places provider -
    # geography is not Mado's data, so there is nothing here to validate it
    # against. Bounded only in length, because a model occasionally returns a
    # sentence where a place name was asked for.
    destination = " ".join((payload.get("destination") or "").split()).strip()
    if destination and len(destination) <= MAX_DESTINATION_LENGTH:
        entities["destination"] = destination

    # Carried as-is; the gateway validates it against the plan actually pending,
    # because the model can only report what it believed was on the table.
    if isinstance(payload.get("refinement"), dict):
        entities["refinement"] = payload["refinement"]

    return Understanding(
        intent=intent,
        confidence=confidence,
        entities=entities,
        constraints=constraints,
        time_window=_window_from(payload, now=now, timezone=timezone),
        preferences=_preferences_from(payload),
        needs_clarification=bool(payload.get("needsClarification")),
        clarification_question=(payload.get("clarificationQuestion") or None),
        requires_tools=intent != intents.GENERAL_ASSISTANCE or bool(query),
    )


def _window_from(
    payload: dict, *, now: datetime, timezone: str
) -> intents.TimeWindow | None:
    raw = payload.get("timeWindow")
    if not isinstance(raw, dict):
        return None

    start = _parse_iso(raw.get("start"))
    end = _parse_iso(raw.get("end"))
    if start is None or end is None or end <= start:
        return None

    # A window that has entirely passed is a misreading, not a request - most often
    # a model resolving "Friday" to the one just gone. Better to drop it and search
    # broadly than to promise events that already happened.
    if end < now:
        logger.warning("understanding_window_in_the_past", start=str(start), end=str(end))
        return None

    return intents.TimeWindow(
        start=max(start, now - timedelta(hours=1)),
        end=end,
        label=(raw.get("label") or "then").strip()[:40],
        confidence=_clamp(payload.get("timeWindowConfidence"), default=1.0),
    )


def _preferences_from(payload: dict) -> list[RevealedPreference]:
    found: list[RevealedPreference] = []
    seen: set[tuple[str, str]] = set()

    for item in payload.get("preferences") or []:
        if not isinstance(item, dict):
            continue
        attribute = (item.get("attribute") or "").strip().lower()
        value = " ".join((item.get("value") or "").split()).strip(" .,!").lower()

        if attribute not in _VALID_ATTRIBUTES or not value:
            continue
        if len(value) < 2 or len(value) > MAX_PREFERENCE_VALUE:
            continue
        if (attribute, value) in seen:
            continue

        seen.add((attribute, value))
        found.append(
            RevealedPreference(
                attribute=attribute,
                value=value,
                confidence=_clamp(item.get("confidence"), default=0.8),
            )
        )
    return found


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clamp(value, *, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


# --- degraded path -----------------------------------------------------------


def _from_rules(
    text: str,
    *,
    now: datetime,
    timezone: str,
    reason: str,
    pending_plan: dict | None = None,
) -> Understanding:
    """Deterministic comprehension, for when the model is unavailable.

    Retained only as a floor. It understands a fraction of what the model does, so
    the result is marked degraded and the caller is expected to be more willing to
    ask a clarifying question rather than act on a shaky reading.
    """
    # Refinement first, and only when there is a plan to refine. Without this the
    # rules read "make it cheaper" as a fresh search and hand back an unrelated
    # list - throwing away the plan the explorer was in the middle of adjusting.
    stops = (pending_plan or {}).get("stops") or []
    if stops:
        read = refinement.read_rules(text, stop_count=len(stops))
        if read is not None:
            logger.info("understanding_degraded_refinement", reason=reason, kind=read.kind)
            return Understanding(
                intent=intents.REFINE_PLAN,
                confidence=intents.CONFIDENCE_MODERATE,
                entities={"refinement": {"kind": read.kind, "stopIndex": read.stop_index,
                                         "budget": read.budget}},
                degraded=True,
                prompt_version=f"{UNDERSTANDING_PROMPT_VERSION}-degraded",
            )

    classification = intents.classify(text, now=now, timezone=timezone)
    logger.info("understanding_degraded", reason=reason, intent=classification.intent)

    return Understanding(
        intent=classification.intent,
        # Capped below the high-confidence band: a rule matching a pattern is not
        # evidence the explorer meant it, only that they phrased it expectedly.
        confidence=min(classification.confidence, intents.CONFIDENCE_MODERATE),
        entities=classification.entities,
        constraints=classification.constraints,
        time_window=classification.time_window,
        # No preference extraction in this mode. Guessing at what someone revealed
        # about themselves from keywords is how "I don't love crowds" becomes a
        # stored liking for crowds - and a bad memory outlives the outage.
        preferences=[],
        requires_tools=classification.requires_tools,
        degraded=True,
        prompt_version=f"{UNDERSTANDING_PROMPT_VERSION}-degraded",
    )
