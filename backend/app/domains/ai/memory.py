"""Conversational memory.

Spec 54.05 and 56.01 s18 describe memory that persists across conversations, is
split by authority, and decays. Three rules follow from those specs and shape
everything here:

* **Explicit statements outrank inferences.** "I'm vegetarian" is a fact the
  explorer told us; "clicked three vegetarian places" is a guess. They are stored
  with different sources, different confidence and different decay rates, and the
  explicit one wins whenever they conflict.

* **Memory decays.** A preference stated eight months ago and never repeated
  should stop steering results. Confidence is therefore read through
  :func:`effective_confidence`, never straight off the column - the stored value
  is what we learned, not what it is worth today.

* **Memory is opt-in and revocable** (spec PRODUCT-00 principle 9). Every entry
  point checks ``aiMemoryEnabled`` first, so turning it off stops both writing and
  reading immediately, without a migration or a cleanup job.

Recall is semantic rather than keyword-based. That is the whole reason memory
waited on embeddings: "somewhere for dinner" has to surface "I'm vegetarian", and
no amount of string matching connects those two phrases.

Extraction lives elsewhere. Reading what someone revealed about themselves is a
language problem, and :mod:`app.domains.ai.understanding` solves it with a model:
"I don't eat meat" and "peanuts nearly killed me once" carry preferences that no
pattern list recognises. This module receives an already-understood list and owns
what happens next - what to keep, what to reinforce, how it decays and when it is
recalled.

The pattern-based :func:`extract` below survives only as an offline fallback. It
runs when no provider is configured, and its limitations are documented there
rather than hidden, because a concierge that remembers a little during an outage
beats one that silently remembers nothing.

Either way the constraint from spec 56.01 s3.1 holds: nothing here decides a fact
about the world. A memory records what the explorer said about *themselves*, and
recommendations are still built from retrieved catalogue data.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.ai.models import SOURCE_EXPLICIT, SOURCE_INFERRED, UserMemory
from app.integrations.embeddings import TASK_DOCUMENT, TASK_QUERY, get_embedding_provider

logger = get_logger("mado.ai.memory")

# Half-life of a memory's confidence, in days. Explicit statements are held far
# longer: someone who says they are vegetarian is unlikely to have changed their
# mind by next month, whereas a behavioural inference drawn from a few taps is a
# weak signal that should fade if it is never reinforced.
HALF_LIFE_DAYS = {SOURCE_EXPLICIT: 365.0, SOURCE_INFERRED: 45.0}

# Below this, a memory no longer influences anything. Kept rather than deleted so
# it can be reinforced back to relevance - and so the explorer can still see and
# delete it, which they cannot do with a row that silently vanished.
MIN_USEFUL_CONFIDENCE = 0.15

# Which kind of memory each attribute represents. Dietary facts are their own type
# because they are safety-relevant and always recalled - see
# ALWAYS_RECALLED_ATTRIBUTES.
_TYPE_BY_ATTRIBUTE = {
    "restriction": "dietary",
    "allergy": "dietary",
    "likes": "preference",
    "dislikes": "preference",
    "avoids": "preference",
    "companions": "context",
    "budget": "constraint",
}

# Cosine distance within which two memories are considered the same statement.
# Tight on purpose: merging "I love spicy food" into "I hate spicy food" because
# they are lexically similar would be far worse than storing both.
DUPLICATE_DISTANCE = 0.12

# How many memories to put in front of the model. A prompt stuffed with thirty
# half-relevant facts produces worse answers than one holding the three that
# actually bear on the question.
RECALL_LIMIT = 5

# Memories that bypass relevance ranking entirely and are always recalled.
#
# Semantic ranking is the right default, but it is the wrong mechanism for these.
# An allergy is not "relevant to food questions" - it is relevant to every
# recommendation that could end in someone eating something, which includes "what
# is on tonight" and "somewhere for a drink". The cost of carrying an allergy in a
# prompt that did not need it is a few wasted tokens; the cost of dropping one
# because the query did not sound food-related is an unsafe recommendation.
#
# Kept deliberately narrow: only facts where omission is harmful rather than
# merely unhelpful.
ALWAYS_RECALLED_ATTRIBUTES = frozenset({"allergy", "restriction"})


@dataclass(slots=True)
class Extracted:
    """A candidate memory found in a message, before it is stored."""

    type: str
    category: str | None
    attribute: str | None
    value: str
    confidence: float
    source: str


# --- extraction --------------------------------------------------------------
#
# Each pattern captures the *subject* of a preference. They are deliberately
# anchored to first-person statements: "I love jazz" is a preference, "the jazz
# night was busy" is not, and a looser pattern cannot tell them apart.

_DIETARY_TERMS = (
    "vegetarian|vegan|pescatarian|halal|kosher|gluten[- ]free|lactose[- ]intolerant"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str, str | None, float, str], ...] = (
    # (pattern, type, attribute, confidence, polarity-carrying group name)
    (
        re.compile(rf"\bi(?:'m| am)\s+(?:a\s+)?({_DIETARY_TERMS})\b", re.I),
        "dietary",
        "restriction",
        0.95,
        "value",
    ),
    (
        re.compile(
            r"\bi(?:'m| am)\s+allergic\s+to\s+"
            r"([a-z][a-z\s]{1,28}?)\b(?:\.|,|$|\s+and\b)",
            re.I,
        ),
        "dietary",
        "allergy",
        0.97,
        "value",
    ),
    (
        re.compile(
            r"\bi\s+(?:really\s+)?(?:love|adore|am into)\s+"
            r"([a-z][a-z\s]{1,28}?)\b(?:\.|,|!|$|\s+and\b)",
            re.I,
        ),
        "preference",
        "likes",
        0.85,
        "value",
    ),
    (
        re.compile(
            r"\bi\s+(?:really\s+)?(?:hate|dislike|can't stand|cannot stand)\s+"
            r"([a-z][a-z\s]{1,28}?)\b(?:\.|,|!|$|\s+and\b)",
            re.I,
        ),
        "preference",
        "dislikes",
        0.88,
        "value",
    ),
    (
        re.compile(r"\bi\s+(?:don't|do not)\s+(drink|smoke|eat meat|like crowds)\b", re.I),
        "preference",
        "avoids",
        0.9,
        "value",
    ),
    (
        re.compile(r"\bi\s+prefer\s+([a-z][a-z\s]{1,28}?)\b(?:\.|,|$|\s+and\b|\s+over\b)", re.I),
        "preference",
        "likes",
        0.82,
        "value",
    ),
    (
        re.compile(
            r"\bi(?:'m| am)\s+(?:travell?ing|here)\s+with\s+"
            r"(my\s+[a-z][a-z\s]{1,24}?|kids|family|friends)\b(?:\.|,|$)",
            re.I,
        ),
        "context",
        "companions",
        0.8,
        "value",
    ),
    (
        re.compile(r"\bi(?:'m| am)\s+on\s+a\s+(tight|small|limited)\s+budget\b", re.I),
        "constraint",
        "budget",
        0.85,
        "value",
    ),
)

# Fragments that pass the patterns but carry no durable meaning. Storing "I love
# it" as a preference for "it" is worse than storing nothing.
_JUNK_VALUES = frozenset(
    """it this that them these those there here you me us him her thing things
    stuff something anything everything one ones what which when where how""".split()  # noqa: SIM905 - reads better than 90 quoted strings
)


def extract(text: str) -> list[Extracted]:
    """Find durable preferences stated in a message, by pattern.

    **Fallback only.** Comprehension belongs to the model
    (:mod:`app.domains.ai.understanding`); this runs when no provider is available.

    The limits are the reason it was demoted: it recognises "I'm vegetarian" but
    not "I don't eat meat", "I love jazz" but not "jazz is my whole thing", and it
    cannot read a negation it was not written to expect. Every phrasing it misses
    is invisible until a real person uses it. It is kept because a concierge that
    remembers a little during an outage beats one that silently remembers nothing,
    not because pattern matching is an acceptable way to read language.
    """
    found: list[Extracted] = []
    seen: set[tuple[str | None, str]] = set()

    for pattern, mem_type, attribute, confidence, _group in _PATTERNS:
        for match in pattern.finditer(text):
            value = " ".join(match.group(1).split()).strip(" .,!").lower()
            if not value or value in _JUNK_VALUES or len(value) < 3:
                continue
            key = (attribute, value)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Extracted(
                    type=mem_type,
                    category=mem_type,
                    attribute=attribute,
                    value=value,
                    confidence=confidence,
                    source=SOURCE_EXPLICIT,
                )
            )
    return found


# --- decay -------------------------------------------------------------------


def effective_confidence(memory: UserMemory, *, now: datetime | None = None) -> float:
    """Confidence discounted for how long it has gone unreinforced.

    Exponential decay on a per-source half-life. Exponential rather than linear
    because forgetting is proportional: a memory does not become worthless on a
    particular day, it becomes steadily less load-bearing.
    """
    now = now or datetime.now(UTC)
    reference = memory.last_reinforced_at or memory.created_at
    if reference is None:
        return float(memory.confidence)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    age_days = max(0.0, (now - reference).total_seconds() / 86400.0)
    half_life = HALF_LIFE_DAYS.get(memory.source, HALF_LIFE_DAYS[SOURCE_INFERRED])
    return float(memory.confidence) * (0.5 ** (age_days / half_life))


# --- service -----------------------------------------------------------------


def memory_enabled(privacy: dict | None) -> bool:
    """Whether memory may be read or written for this explorer.

    Defaults to enabled to match :data:`DEFAULT_PRIVACY`, but an explicit ``False``
    always wins - including over a missing profile, which is treated as "no
    consent recorded" only when the caller passes ``None`` for an anonymous user.
    """
    if privacy is None:
        return False
    return bool(privacy.get("aiMemoryEnabled", True))


class MemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def remember_revealed(
        self,
        user_id: uuid.UUID,
        revealed: list,
        *,
        privacy: dict | None,
    ) -> list[UserMemory]:
        """Store preferences the comprehension layer read from a message.

        Takes an already-understood list rather than raw text. Working out what
        someone revealed about themselves is a language task and belongs to the
        model (see :mod:`app.domains.ai.understanding`); this module's job is
        deciding what to keep, what to reinforce and how it decays.
        """
        candidates = [
            Extracted(
                type=_TYPE_BY_ATTRIBUTE.get(item.attribute, "preference"),
                category=_TYPE_BY_ATTRIBUTE.get(item.attribute, "preference"),
                attribute=item.attribute,
                value=item.value,
                confidence=item.confidence,
                # Read from a first-person statement, so this is the explorer
                # speaking - not the platform inferring from behaviour.
                source=SOURCE_EXPLICIT,
            )
            for item in revealed
        ]
        return await self._store(user_id, candidates, privacy=privacy)

    async def remember(
        self,
        user_id: uuid.UUID,
        text: str,
        *,
        privacy: dict | None,
    ) -> list[UserMemory]:
        """Extract preferences from raw text using the deterministic rules.

        Only for the degraded path, where no model is available to read the
        message. See :func:`extract` for what that costs.
        """
        return await self._store(user_id, extract(text), privacy=privacy)

    async def _store(
        self,
        user_id: uuid.UUID,
        candidates: list[Extracted],
        *,
        privacy: dict | None,
    ) -> list[UserMemory]:
        if not memory_enabled(privacy):
            return []
        if not candidates:
            return []

        provider = get_embedding_provider()
        phrases = [_phrase(c) for c in candidates]
        try:
            batch = await provider.embed(phrases, task=TASK_DOCUMENT)
        except Exception as exc:  # noqa: BLE001 - memory is an enhancement, never a blocker
            logger.warning("memory_embed_failed", error=str(exc))
            return []

        existing = await self._all_for(user_id)
        stored: list[UserMemory] = []
        now = datetime.now(UTC)

        for candidate, vector in zip(candidates, batch.vectors, strict=False):
            match = _find_duplicate(candidate, existing, vector, batch.model)
            if match is not None:
                # Reinforcement, not replacement: hearing something twice should
                # make it more durable, and resets the decay clock.
                match.confidence = min(1.0, float(match.confidence) + 0.05)
                match.last_reinforced_at = now
                if candidate.source == SOURCE_EXPLICIT:
                    match.source = SOURCE_EXPLICIT
                stored.append(match)
                continue

            memory = UserMemory(
                user_id=user_id,
                type=candidate.type,
                category=candidate.category,
                attribute=candidate.attribute,
                value=candidate.value,
                confidence=candidate.confidence,
                source=candidate.source,
                embedding=vector,
                embedding_model=batch.model,
                last_reinforced_at=now,
            )
            self.session.add(memory)
            existing.append(memory)
            stored.append(memory)

        logger.info("memory_written", user_id=str(user_id), count=len(stored))
        return stored

    async def recall(
        self,
        user_id: uuid.UUID,
        query: str,
        *,
        privacy: dict | None,
        limit: int = RECALL_LIMIT,
    ) -> list[UserMemory]:
        """Memories relevant to what the explorer just asked.

        Ranked by semantic closeness weighted by decayed confidence, so a strongly
        held old preference and a loosely held recent one compete on equal terms.
        """
        if not memory_enabled(privacy):
            return []

        memories = await self._all_for(user_id)
        if not memories:
            return []

        now = datetime.now(UTC)
        live = [
            memory
            for memory in memories
            if effective_confidence(memory, now=now) >= MIN_USEFUL_CONFIDENCE
        ]
        if not live:
            return []

        # Safety-critical facts are carried regardless of what was asked.
        always = [m for m in live if m.attribute in ALWAYS_RECALLED_ATTRIBUTES]
        rankable = [m for m in live if m.attribute not in ALWAYS_RECALLED_ATTRIBUTES]
        remaining = max(0, limit - len(always))
        if not rankable or remaining == 0:
            return always[:limit] if not rankable else always

        provider = get_embedding_provider()
        try:
            batch = await provider.embed([query], task=TASK_QUERY)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_recall_embed_failed", error=str(exc))
            return always + _by_confidence(rankable, now=now, limit=remaining)

        query_vector = batch.vectors[0]
        scored: list[tuple[float, UserMemory]] = []
        for memory in rankable:
            # Only vectors from the same model are comparable. Anything else falls
            # back to confidence ordering rather than being scored against a space
            # it does not belong to.
            if memory.embedding is None or memory.embedding_model != batch.model:
                continue
            similarity = _cosine(query_vector, list(memory.embedding))
            scored.append((similarity * effective_confidence(memory, now=now), memory))

        if not scored:
            return always + _by_confidence(rankable, now=now, limit=remaining)

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return always + [memory for _, memory in scored[:remaining]]

    async def forget(self, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        """Delete one memory outright.

        A hard delete, not a soft one. Spec PRODUCT-00 principle 9 makes memory
        revocable, and "revoked" has to mean the row is gone - a flagged row is
        still a record of something the explorer asked us to forget.
        """
        memory = await self.session.get(UserMemory, memory_id)
        if memory is None or memory.user_id != user_id:
            return False
        await self.session.delete(memory)
        return True

    async def forget_all(self, user_id: uuid.UUID) -> int:
        """Clear every memory, for when an explorer switches memory off."""
        memories = await self._all_for(user_id)
        for memory in memories:
            await self.session.delete(memory)
        return len(memories)

    async def list_for(self, user_id: uuid.UUID) -> list[UserMemory]:
        """Every memory held for an explorer, including faded ones.

        Faded entries are included deliberately: this backs the "what do you know
        about me" surface, and hiding a low-confidence memory there would mean
        holding data the explorer cannot see or delete.
        """
        result = await self.session.execute(
            select(UserMemory).where(UserMemory.user_id == user_id)
        )
        return list(result.scalars().all())

    # Internal alias kept for the recall/write paths, which want the same query
    # without implying the "show the explorer everything" contract.
    _all_for = list_for


# --- helpers -----------------------------------------------------------------


def _phrase(candidate: Extracted) -> str:
    """Render a memory as the sentence it came from.

    Embedded as prose rather than as ``attribute=value`` because the vector space
    is built from natural language: "dislikes crowds" sits near "somewhere quiet",
    while the literal string "dislikes=crowds" sits nowhere useful.
    """
    templates = {
        "likes": "I like {value}",
        "dislikes": "I dislike {value}",
        "avoids": "I avoid {value}",
        "restriction": "I am {value}",
        "allergy": "I am allergic to {value}",
        "companions": "I am travelling with {value}",
        "budget": "I am on a {value} budget",
    }
    return templates.get(candidate.attribute or "", "{value}").format(value=candidate.value)


def _find_duplicate(
    candidate: Extracted,
    existing: list[UserMemory],
    vector: list[float],
    model: str,
) -> UserMemory | None:
    """Locate the memory a candidate should reinforce, if any.

    Exact attribute+value matches first, because they are unambiguous. Semantic
    matching only within the same attribute: "likes jazz" and "dislikes jazz"
    embed close together, and treating one as a repeat of the other would let a
    stated dislike quietly reinforce its opposite.
    """
    for memory in existing:
        if memory.attribute == candidate.attribute and memory.value == candidate.value:
            return memory

    for memory in existing:
        if memory.attribute != candidate.attribute:
            continue
        if memory.embedding is None or memory.embedding_model != model:
            continue
        if 1.0 - _cosine(vector, list(memory.embedding)) <= DUPLICATE_DISTANCE:
            return memory
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    """Dot product. Both providers return unit vectors, so this is cosine."""
    return sum(x * y for x, y in zip(a, b, strict=False))


def _by_confidence(
    memories: list[UserMemory], *, now: datetime, limit: int
) -> list[UserMemory]:
    """Fallback ordering when vectors are unavailable or incomparable."""
    ranked = sorted(memories, key=lambda m: effective_confidence(m, now=now), reverse=True)
    return ranked[:limit]


def render_for_prompt(memories: list[UserMemory]) -> str:
    """Format recalled memories for the system prompt.

    Confidence is stated so the model can hedge appropriately, and the source is
    stated so it can tell "you told me" from "I noticed" - claiming an explorer
    said something they never said is the specific failure this prevents.
    """
    if not memories:
        return ""
    lines = []
    for memory in memories:
        origin = "stated" if memory.is_explicit else "inferred"
        lines.append(f"- {_phrase_of(memory)} ({origin})")
    return "What you know about this explorer:\n" + "\n".join(lines)


def _phrase_of(memory: UserMemory) -> str:
    return _phrase(
        Extracted(
            type=memory.type,
            category=memory.category,
            attribute=memory.attribute,
            value=memory.value,
            confidence=float(memory.confidence),
            source=memory.source,
        )
    )
