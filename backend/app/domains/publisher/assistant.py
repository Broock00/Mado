"""The publisher content assistant (spec PUB-005).

Helps somebody write a clearer listing. Suggests a card summary, tightens their
description, proposes a category and tags, and asks what a reader would still
want to know.

**Nothing is applied automatically.** Every suggestion comes back for the
publisher to accept, edit or ignore. Spec AI-26 §2: AI enhances human decisions
rather than replacing them, and a listing is the publisher's word about their own
place - the platform has no business rewriting it behind their back.

**The model may not add facts, and that is enforced rather than requested.**

The prompt says so at length, which is necessary and nowhere near sufficient. A
model asked to improve "small cafe on Taitu Street" will cheerfully return
"cosy cafe on Taitu Street, open from 7am, coffee around 50 birr" - fluent,
plausible, and entirely invented. Published, that sends somebody across the city
at eight in the morning expecting a price nobody quoted.

So every suggestion is checked against the source before it is offered:

* :func:`introduces_numbers` rejects a rewrite containing any number that was not
  in the draft. Prices, times, capacities and distances are the facts that cause
  real harm when wrong, and they are all numbers - which makes this crude check
  cover most of the damage.
* :func:`introduces_claims` rejects one that adds free/paid language, because
  "free entry" is the single most costly hallucination available and it need not
  contain a digit.
* Category and tag suggestions are matched against the real catalogue, so an
  invented slug is dropped rather than shown.

A failed check drops that one suggestion and keeps the rest. The publisher is
never told "the assistant misbehaved" - they simply get the suggestions that
survived, which is the honest outcome.

Spec 56.01 §3.1, tool-first factuality: the model phrases, it does not decide
facts. Here it does not even get to introduce them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.ai.prompts import (
    ASSISTANT_PROMPT_VERSION,
    ASSISTANT_RESPONSE_SCHEMA,
    ASSISTANT_SYSTEM_PROMPT,
)
from app.domains.catalog.models import Category, Tag
from app.integrations.ai_provider import GenerationRequest, get_provider

logger = get_logger("mado.publisher.assistant")

MAX_INPUT = 6000
MAX_SUMMARY = 200
MAX_QUESTIONS = 4
MAX_TAGS = 4

# Digits, and the number words a rewrite might use to smuggle one past a digit
# check ("opens at seven"). Not exhaustive and does not need to be: it raises the
# cost of an invented fact rather than claiming to make one impossible.
_NUMERIC = re.compile(r"\d")
_NUMBER_WORDS = frozenset(
    """one two three four five six seven eight nine ten eleven twelve twenty
    thirty forty fifty hundred thousand dozen half quarter""".split()  # noqa: SIM905
)

# Language that asserts a price where the draft asserted none. "Free" is the
# expensive one - somebody turns up with no money.
_PRICE_CLAIMS = re.compile(
    r"\b(free|no charge|no cost|complimentary|paid|ticketed|entry fee|birr|etb)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-zሀ-፿]+")


def _words(text: str) -> set[str]:
    """Lowercased word set, including Amharic script.

    The Ethiopic range matters: without it every Amharic word in a rewrite would
    look like an addition and the grounding check would reject every suggestion
    on an Amharic draft.
    """
    return set(_WORD.findall(text.lower()))


def introduces_numbers(source: str, suggestion: str) -> bool:
    """Whether the suggestion contains a number the source did not.

    Deliberately strict about digits: any digit not present in the source counts,
    without trying to parse what it means. A rewrite has no legitimate reason to
    introduce "7" to a draft that never mentioned it.
    """
    source_digits = set(_NUMERIC.findall(source))
    if any(digit not in source_digits for digit in _NUMERIC.findall(suggestion)):
        return True

    added_words = _words(suggestion) - _words(source)
    return bool(added_words & _NUMBER_WORDS)


def introduces_claims(source: str, suggestion: str) -> bool:
    """Whether the suggestion asserts a price the source did not."""
    if _PRICE_CLAIMS.search(suggestion) is None:
        return False
    return _PRICE_CLAIMS.search(source) is None


def drops_facts(source: str, suggestion: str) -> bool:
    """Whether the rewrite silently lost a number the draft had.

    The counterpart to :func:`introduces_numbers`, and it was not obvious it was
    needed until the assistant was pointed at a real draft. Told firmly enough
    not to invent prices and times, the model started omitting the ones the
    publisher had written - and then listed them under "missing", asking for
    detail it had just deleted.

    That is worse than inventing, in one specific way: a publisher who accepts
    an invented fact can usually see it is wrong, while a publisher who accepts
    a shorter, tidier paragraph will not notice the door time has gone.
    """
    return bool(set(_NUMERIC.findall(source)) - set(_NUMERIC.findall(suggestion)))


def is_grounded(source: str, suggestion: str, *, must_keep_facts: bool = True) -> bool:
    """Whether a rewrite stays inside what the draft said.

    `must_keep_facts` is off for the summary and on for the description. A card
    summary is one sentence under 140 characters - leaving out the door time is
    the entire job, not a failure. Applying fact retention to it rejected every
    summary of every draft that mentioned a number, which is most of them.
    """
    if introduces_numbers(source, suggestion) or introduces_claims(source, suggestion):
        return False
    return not (must_keep_facts and drops_facts(source, suggestion))


@dataclass(slots=True)
class Suggestions:
    """What survived the checks. Any field may be absent."""

    summary: str | None = None
    description: str | None = None
    category_slug: str | None = None
    tags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # False when there is no model to ask. The interface hides the feature rather
    # than offering a button that does nothing.
    available: bool = True
    # Suggestions the grounding check threw away. Surfaced for logging and tests,
    # never shown to the publisher - "the assistant tried to invent a price" is
    # our problem, not theirs.
    rejected: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.summary, self.description, self.category_slug, self.tags, self.missing)
        )


class ContentAssistant:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def suggest(
        self,
        *,
        title: str,
        description: str,
        summary: str | None = None,
    ) -> Suggestions:
        provider = get_provider()
        if getattr(provider, "name", "stub") == "stub":
            return Suggestions(available=False)

        categories = {c.slug: c.name for c in await self._categories()}
        tags = {t.slug: t.name for t in await self._tags()}

        body = "\n".join(
            [
                f"TITLE: {title}",
                f"CURRENT SUMMARY: {summary or '(none)'}",
                f"DESCRIPTION: {description}",
                "",
                "CATEGORIES YOU MAY CHOOSE FROM: "
                + ", ".join(f"{slug} ({name})" for slug, name in categories.items()),
                "TAGS YOU MAY CHOOSE FROM: " + ", ".join(tags),
            ]
        )[:MAX_INPUT]

        request = GenerationRequest(
            system_prompt=ASSISTANT_SYSTEM_PROMPT,
            # The draft travels as the user turn. Anything instruction-shaped
            # inside it is then plainly a publisher's prose rather than direction.
            user_message=body,
            response_schema=ASSISTANT_RESPONSE_SCHEMA,
            # Low but not zero. This is writing, and zero produces the same
            # flattened phrasing for every listing in the city.
            temperature=0.3,
            max_output_tokens=1200,
            thinking=False,
        )

        try:
            result = await provider.generate(request, model=_fast_model())
        except Exception as exc:  # noqa: BLE001 - a draft must still be savable
            logger.warning("content_assistant_failed", error=str(exc))
            return Suggestions(available=False)

        payload = result.structured
        if not isinstance(payload, dict):
            logger.warning("content_assistant_unparseable", model=result.model)
            return Suggestions(available=False)

        return self._validate(
            payload,
            source=f"{title}\n{summary or ''}\n{description}",
            categories=categories,
            tags=tags,
        )

    # ----------------------------------------------------------- validation

    def _validate(
        self,
        payload: dict,
        *,
        source: str,
        categories: dict[str, str],
        tags: dict[str, str],
    ) -> Suggestions:
        rejected: list[str] = []

        suggested_summary = _clean(payload.get("summary"), MAX_SUMMARY)
        if suggested_summary and not is_grounded(
            source, suggested_summary, must_keep_facts=False
        ):
            rejected.append("summary")
            suggested_summary = None

        suggested_description = _clean(payload.get("description"), MAX_INPUT)
        if suggested_description and not is_grounded(source, suggested_description):
            rejected.append("description")
            suggested_description = None

        # A closed set, checked against the real catalogue. An invented slug is
        # dropped rather than shown and then rejected on save.
        slug = payload.get("categorySlug")
        category_slug = slug if isinstance(slug, str) and slug in categories else None
        if slug and category_slug is None:
            rejected.append(f"category:{slug}")

        suggested_tags = [
            t for t in (payload.get("tags") or []) if isinstance(t, str) and t in tags
        ][:MAX_TAGS]

        questions = [
            cleaned
            for cleaned in (_clean(q, 160) for q in (payload.get("missing") or []))
            if cleaned
        ][:MAX_QUESTIONS]

        if rejected:
            # Logged loudly. A model that keeps trying to invent prices is a
            # prompt problem, and this is the only place it would ever show up.
            logger.warning(
                "content_assistant_suggestion_rejected",
                rejected=rejected,
                prompt_version=ASSISTANT_PROMPT_VERSION,
            )

        return Suggestions(
            summary=suggested_summary,
            description=suggested_description,
            category_slug=category_slug,
            tags=suggested_tags,
            missing=questions,
            rejected=rejected,
        )

    async def _categories(self) -> list[Category]:
        return list((await self.session.execute(select(Category))).scalars())

    async def _tags(self) -> list[Tag]:
        return list((await self.session.execute(select(Tag))).scalars())


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def _fast_model() -> str:
    from app.core.config import get_settings

    return get_settings().ai_fast_model
