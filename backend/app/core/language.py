"""Which language to answer in (spec 11.07 "Language Accessibility", 18 §15).

Mado's pilot city is Addis Ababa, so the two languages that matter are English
and Amharic. Everything here is written so a third is a data change rather than
a code change.

**The server translates almost nothing, and that is the design.** API errors
carry a stable `code` alongside English prose, and the client turns the code
into a sentence in the reader's language. Translating on the server would mean
every locale round-tripping through the API for text that never changes, and a
client that already has to render its own buttons in Amharic gaining a second,
inconsistent source of Amharic.

The exception is anything the server *writes down* rather than returns:
notification titles and bodies are rendered once, stored, and read back weeks
later - possibly by a background job with no request attached. Those have to be
in the recipient's language at the moment they are written, which is why there
is a small catalogue here and nowhere else.

**A stated preference beats a browser header.** Somebody who chose Amharic in
their settings means it on a borrowed laptop whose browser asks for English.
`Accept-Language` is only consulted when nobody has said.
"""

from __future__ import annotations

import re

DEFAULT = "en"

# Supported languages, in the order they are offered. Amharic is second only
# because English is the fallback, not because it is secondary here.
SUPPORTED: tuple[str, ...] = ("en", "am")

LANGUAGE_NAMES = {
    # Each in its own language, never translated. A language picker that says
    # "Amharic" to somebody who does not read English has failed at the one job
    # it has.
    "en": "English",
    "am": "አማርኛ",
}

# `am-ET;q=0.9` - the tag, and an optional quality weight.
_ENTRY = re.compile(
    r"^\s*([A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*|\*)\s*(?:;\s*q\s*=\s*([0-9.]+))?\s*$"
)


def normalise(tag: str | None) -> str | None:
    """`am-ET` -> `am`, and anything unsupported -> None.

    Only the primary subtag is kept. Mado has one translation of Amharic, and
    pretending to distinguish `am-ET` from `am` would be a promise about
    regional variants that nothing behind it can keep.
    """
    if not tag:
        return None
    primary = tag.strip().split("-")[0].lower()
    return primary if primary in SUPPORTED else None


def from_accept_language(header: str | None) -> str | None:
    """The best supported match from an `Accept-Language` header.

    Ordered by the client's own quality weights, so a browser that says
    `am;q=0.9, en;q=0.8` gets Amharic even though English appears in the list.
    A malformed entry is skipped rather than failing the parse - this arrives
    from the open internet and is not worth an error.
    """
    if not header:
        return None

    ranked: list[tuple[float, int, str]] = []
    for index, raw in enumerate(header.split(",")):
        match = _ENTRY.match(raw)
        if not match:
            continue
        tag, weight = match.group(1), match.group(2)
        try:
            quality = float(weight) if weight is not None else 1.0
        except ValueError:
            continue
        if quality <= 0:
            # `q=0` means "explicitly not this one".
            continue
        # Index breaks ties in the order the client listed them, which is the
        # order the specification says to prefer.
        ranked.append((-quality, index, tag))

    for _, _, tag in sorted(ranked):
        if tag == "*":
            return DEFAULT
        supported = normalise(tag)
        if supported:
            return supported
    return None


def resolve(stated: str | None, accept_language: str | None) -> str:
    """The language to use: what they chose, what they asked for, or English."""
    return normalise(stated) or from_accept_language(accept_language) or DEFAULT
