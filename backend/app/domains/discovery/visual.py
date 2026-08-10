"""Visual search (spec SRCH-005).

Point a camera at something and find places like it.

**The model describes; the catalogue decides what exists.** This is spec 56.01
§3.1 applied to vision. The model is asked what *kind* of place or thing the
photograph shows - "a traditional coffee house, low wooden stools, roasting
over coals" - and that description is then run through the search Mado already
has. The model never names a venue, and if it does the name is discarded before
the search runs. A model that recognises the National Museum from a photograph
is impressive; a model that confidently misidentifies a building and sends
somebody across Addis to the wrong place is a bug wearing a demo's clothes.

**So this answers "what is this like", not "what is this".** The distinction
matters because it is not what people expect. Somebody photographing a specific
building wants to be told which building it is, and the honest answer is
usually that we cannot know - but here are places of that kind. The interface
says so in those words rather than presenting similarity as identification. If
the actual place is in the catalogue it tends to surface anyway, through the
same similarity that finds everything else.

**The photograph is never stored.** It is decoded to check it is an image,
re-encoded to strip metadata, sent to the model, and dropped. Photographs taken
in a city contain people, homes and number plates that nobody consented to hand
over, and a search feature has no reason to keep any of it. Nothing here writes
to disk or to the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.integrations.ai_provider import GenerationRequest, get_provider
from app.integrations.media_storage import process_image

logger = get_logger("mado.visual_search")

# Deliberately small. A description is a handful of words for retrieval, not a
# caption - and a long one drifts into narration the search cannot use.
MAX_OUTPUT_TOKENS = 220

# Below this the model is telling us it cannot make out the subject, and
# searching on a guess wastes the explorer's attention on results chosen by
# nothing.
MIN_CONFIDENCE = 0.35

SYSTEM_PROMPT = """You look at a photograph and say what kind of place or thing \
it shows, in the plain words somebody would use to search for one.

Rules you must follow:
- Never name a specific business, venue, landmark or person. If you recognise \
one, describe its type instead.
- Describe only what is visible. Do not infer a city, a country or an occasion.
- If the photograph is too dark, blurred or ambiguous to tell, say so and give \
a low confidence rather than guessing.
- Keep the description under 20 words.
- Write everything in lower case, including the first word."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "What kind of place or thing this is, under 20 words.",
        },
        "terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three to six plain search words.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. How clearly the subject can be made out.",
        },
    },
    # Marked required so the model cannot omit a field and leave the caller
    # guessing whether it declined or simply forgot - the same problem the
    # publisher assistant hit.
    "required": ["description", "terms", "confidence"],
}

# Any word carrying a capital is treated as a name the model was told not to
# produce.
#
# This is exact rather than heuristic *because* the prompt asks for lower case
# throughout. An earlier version exempted the first word - the usual way to
# avoid mangling a sentence - and that exemption defeated the check: a
# description of "Fendika jazz bar" passed through untouched because the name
# happened to come first, and "National Museum" became the fragment "National".
# With lower case required, every capital is a violation and none of them are
# legitimate, so no position needs excusing and nothing real is lost.
_CAPITALISED = re.compile(r"\b\w*[A-Z]\w*\b")


@dataclass(slots=True)
class Look:
    """What the model made of a photograph."""

    description: str
    terms: list[str] = field(default_factory=list)
    confidence: float = 0.0
    #: True when the subject could not be made out well enough to search on.
    unclear: bool = False

    @property
    def query(self) -> str:
        return " ".join(self.terms) if self.terms else self.description


def strip_names(text: str) -> str:
    """Remove any capitalised word.

    The system prompt forbids names and asks for lower case. This is the check
    that does not depend on the model having complied - the same belt and
    braces the publisher assistant applies to numbers it was told not to
    invent. A capital that survives the prompt is either a name or a formatting
    slip, and dropping a word costs one slightly worse search result while
    keeping one risks a confident answer about a place that may not exist.
    """
    cleaned = _CAPITALISED.sub(" ", text)
    # Collapse whatever whitespace the removals left, in one pass rather than a
    # single `replace("  ", " ")` - which leaves three spaces as two.
    return " ".join(cleaned.split())


async def look_at(data: bytes) -> Look:
    """Ask what kind of thing a photograph shows.

    Raises `ValidationError` from `process_image` if the bytes are not a usable
    image - which also strips metadata and bounds the dimensions, so what
    reaches the model is smaller than what was uploaded and carries no EXIF.
    """
    normalised, width, height = process_image(data)

    provider = get_provider()
    result = await provider.generate(
        GenerationRequest(
            system_prompt=SYSTEM_PROMPT,
            user_message="What kind of place or thing is in this photograph?",
            image=(normalised, "image/jpeg"),
            response_schema=RESPONSE_SCHEMA,
            # Description, not invention. A low temperature keeps it close to
            # what is actually in the frame.
            temperature=0.1,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # Nothing to reason about: this is perception, and the thinking
            # budget would come out of the answer.
            thinking=False,
        ),
        model=_vision_model(),
    )

    payload = result.structured or {}
    description = strip_names(str(payload.get("description") or "").strip())
    terms = [
        cleaned
        for term in (payload.get("terms") or [])
        if (cleaned := strip_names(str(term)).lower().strip())
    ]
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    unclear = confidence < MIN_CONFIDENCE or not (description or terms)
    logger.info(
        "visual_search_look",
        confidence=round(confidence, 2),
        unclear=unclear,
        width=width,
        height=height,
        terms=len(terms),
    )
    return Look(description=description, terms=terms, confidence=confidence, unclear=unclear)


def _vision_model() -> str:
    from app.core.config import get_settings

    # The fast model is multimodal and this is a perception task, so the
    # reasoning model would cost more and see the same picture.
    return get_settings().ai_fast_model
