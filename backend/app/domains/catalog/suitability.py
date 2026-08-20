"""What a listing is suitable for, as a controlled vocabulary.

A family arriving in a city asks questions the catalogue could not previously
answer: somewhere with a vegan main, somewhere with a play area, somewhere the
wheelchair gets in, somewhere that will still be serving after sunset. The
comprehension layer has always understood those sentences - it extracts
`restriction=vegan` from "my father doesn't eat animal products" - and then had
nowhere to put them, because nothing in `catalog` recorded the answer. The
constraints were passed to the model as prose hints and the retrieval ignored
them, so the concierge would recommend a steakhouse warmly and mention that it
was a shame about the vegan thing.

**A slug is a claim somebody made, and its absence is not a denial.** This is the
single most important property of the design. A listing that does not say
`vegan` has not said it lacks vegan food; nobody has said anything. Storing
`{"vegan": false}` would invent a fact - the publisher never asserted it - and
would then be indistinguishable from a real refusal. So this is a presence-only
set, and three-valued logic lives in the *query*: asserted, contradicted by
nothing, or simply unknown. :func:`assess` returns that distinction rather than
collapsing it to a boolean.

**Where it is stored.** `catalog.experiences.suitability` and the pre-existing
`catalog.venues.facilities`, both string arrays over this one vocabulary, and the
effective set for a listing is the union of the two. The split is real rather
than bureaucratic: a step-free entrance and a car park belong to the building,
while a vegan menu and a family matinee belong to the event - and the same room
hosts a children's puppet show at 11am and an over-18 comedy night at 9pm, so
attaching either to the venue alone would be wrong half the time.

`accessibility` on both tables is deliberately left alone. It holds descriptive
prose for a details page ("ramp at the side entrance, ask staff"), which is worth
showing and is not something a query can filter on. The filterable claim is a
slug here; the sentence explaining it stays there.

**Unknown slugs are dropped, not stored.** A publisher who types `vegann` has
made a claim nobody can ever query, which is worse than having made none: it
looks recorded. Normalising at the boundary means the set in the database is
always drawn from this file.
"""

from __future__ import annotations

# --- Dietary -----------------------------------------------------------------
#
# `fasting_menu` is not a synonym for vegetarian, though it is often served as
# one. In Ethiopia - where this platform started - a fasting menu is the
# Orthodox *ye'tsom* observance and is reliably vegan on fasting days; during
# Ramadan the relevant fact is usually that a kitchen is serving after sunset at
# all. Both are real requests that "vegetarian" answers badly, so the claim is
# its own slug and the concierge is told to ask which is meant rather than guess.
VEGAN = "vegan"
VEGETARIAN = "vegetarian"
HALAL = "halal"
KOSHER = "kosher"
GLUTEN_FREE = "gluten_free"
NUT_FREE = "nut_free"
DAIRY_FREE = "dairy_free"
FASTING_MENU = "fasting_menu"
ALCOHOL_FREE = "alcohol_free"
SERVES_LATE = "serves_late"

DIETARY = frozenset({
    VEGAN,
    VEGETARIAN,
    HALAL,
    KOSHER,
    GLUTEN_FREE,
    NUT_FREE,
    DAIRY_FREE,
    FASTING_MENU,
    ALCOHOL_FREE,
    SERVES_LATE,
})

# --- Family ------------------------------------------------------------------

CHILDRENS_PLAY_AREA = "childrens_play_area"
CHILD_MENU = "child_menu"
HIGH_CHAIRS = "high_chairs"
BABY_CHANGING = "baby_changing"
CHILD_FRIENDLY = "child_friendly"
PUSHCHAIR_ACCESS = "pushchair_access"

FAMILY = frozenset({
    CHILDRENS_PLAY_AREA,
    CHILD_MENU,
    HIGH_CHAIRS,
    BABY_CHANGING,
    CHILD_FRIENDLY,
    PUSHCHAIR_ACCESS,
})

# --- Access ------------------------------------------------------------------

STEP_FREE_ACCESS = "step_free_access"
ACCESSIBLE_TOILET = "accessible_toilet"
ACCESSIBLE_PARKING = "accessible_parking"
HEARING_LOOP = "hearing_loop"
SIGN_LANGUAGE = "sign_language"
QUIET_SPACE = "quiet_space"

ACCESS = frozenset({
    STEP_FREE_ACCESS,
    ACCESSIBLE_TOILET,
    ACCESSIBLE_PARKING,
    HEARING_LOOP,
    SIGN_LANGUAGE,
    QUIET_SPACE,
})

# --- Comfort -----------------------------------------------------------------
#
# These are what make a weather-aware answer possible at a finer grain than
# indoor/outdoor. A courtyard cafe with shade and heaters is a different
# proposition in August and in January, and `is_indoor` alone cannot say so.
INDOOR_SEATING = "indoor_seating"
OUTDOOR_SEATING = "outdoor_seating"
SHADED_SEATING = "shaded_seating"
HEATED = "heated"
AIR_CONDITIONED = "air_conditioned"
COVERED = "covered"

COMFORT = frozenset({
    INDOOR_SEATING,
    OUTDOOR_SEATING,
    SHADED_SEATING,
    HEATED,
    AIR_CONDITIONED,
    COVERED,
})

# --- Practical ---------------------------------------------------------------

PARKING = "parking"
WIFI = "wifi"
PRAYER_ROOM = "prayer_room"
PET_FRIENDLY = "pet_friendly"
CARD_ACCEPTED = "card_accepted"

PRACTICAL = frozenset({PARKING, WIFI, PRAYER_ROOM, PET_FRIENDLY, CARD_ACCEPTED})

ALL = DIETARY | FAMILY | ACCESS | COMFORT | PRACTICAL

# Claims where being wrong hurts somebody rather than disappointing them. A
# requirement drawn from this set is never relaxed to widen a thin result: an
# empty answer is a bad experience, and a confident wrong one is an allergic
# reaction or a family stranded at the bottom of a flight of stairs.
#
# `halal` and `kosher` are here for the same reason, even though the harm is
# religious rather than medical: both are absolutes to the people who ask, and
# neither is ours to treat as a preference.
SAFETY_CRITICAL = frozenset({
    NUT_FREE,
    GLUTEN_FREE,
    DAIRY_FREE,
    HALAL,
    KOSHER,
    VEGAN,
    STEP_FREE_ACCESS,
    ACCESSIBLE_TOILET,
})

# What a slug is called in a sentence. Held here rather than in the client so the
# concierge, the API and the composer all name a claim the same way - an
# explorer told "step-free access" in one place and "wheelchair accessible" in
# another has to work out whether they are the same promise.
LABELS: dict[str, str] = {
    VEGAN: "vegan options",
    VEGETARIAN: "vegetarian options",
    HALAL: "halal",
    KOSHER: "kosher",
    GLUTEN_FREE: "gluten-free options",
    NUT_FREE: "nut-free kitchen",
    DAIRY_FREE: "dairy-free options",
    FASTING_MENU: "fasting menu",
    ALCOHOL_FREE: "alcohol-free",
    SERVES_LATE: "serves late",
    CHILDRENS_PLAY_AREA: "children's play area",
    CHILD_MENU: "children's menu",
    HIGH_CHAIRS: "high chairs",
    BABY_CHANGING: "baby changing",
    CHILD_FRIENDLY: "good with children",
    PUSHCHAIR_ACCESS: "pushchair access",
    STEP_FREE_ACCESS: "step-free access",
    ACCESSIBLE_TOILET: "accessible toilet",
    ACCESSIBLE_PARKING: "accessible parking",
    HEARING_LOOP: "hearing loop",
    SIGN_LANGUAGE: "sign language",
    QUIET_SPACE: "quiet space",
    INDOOR_SEATING: "indoor seating",
    OUTDOOR_SEATING: "outdoor seating",
    SHADED_SEATING: "shade",
    HEATED: "heated",
    AIR_CONDITIONED: "air conditioning",
    COVERED: "covered",
    PARKING: "parking",
    WIFI: "wifi",
    PRAYER_ROOM: "prayer room",
    PET_FRIENDLY: "pets welcome",
    CARD_ACCEPTED: "cards accepted",
}

# Requesting the first implies the rest are acceptable answers. One-directional
# and deliberately sparse: everything vegan is also vegetarian, so somebody who
# needs vegetarian food is served by a vegan kitchen, but the reverse is false
# and encoding it would poison a vegan's results with cheese.
IMPLIES: dict[str, frozenset[str]] = {
    VEGAN: frozenset({VEGETARIAN, DAIRY_FREE}),
    CHILDRENS_PLAY_AREA: frozenset({CHILD_FRIENDLY}),
    CHILD_MENU: frozenset({CHILD_FRIENDLY}),
    HIGH_CHAIRS: frozenset({CHILD_FRIENDLY}),
}


def label(slug: str) -> str:
    """Human wording for a slug, falling back to a readable form of the slug."""
    return LABELS.get(slug, slug.replace("_", " "))


def describe(slugs: list[str] | set[str] | None) -> str:
    """A comma-joined phrase naming what a listing claims."""
    return ", ".join(label(slug) for slug in normalise(slugs))


def normalise(values: list[str] | set[str] | None) -> list[str]:
    """Clean an incoming set of claims down to known slugs, sorted.

    Sorted so two listings with the same claims compare equal and a diff of a
    listing's history does not show a reordering as a change. Unknown slugs are
    dropped rather than kept: see the module docstring.
    """
    if not values:
        return []
    seen = {
        str(value).strip().lower().replace("-", "_").replace(" ", "_")
        for value in values
    }
    return sorted(seen & ALL)


def expand(required: list[str] | set[str] | None) -> set[str]:
    """A requirement plus everything that would also satisfy it.

    Used when matching, never when storing. Somebody who asked for vegetarian
    food is satisfied by a kitchen that claimed vegan, and a query that did not
    know this would drop the best answer it had.
    """
    wanted = set(normalise(required))
    satisfying: set[str] = set(wanted)
    for slug, implied in IMPLIES.items():
        if wanted & implied:
            satisfying.add(slug)
    return satisfying


def effective(experience) -> list[str]:
    """Everything a listing claims, from the experience and its venue.

    The union is what an explorer actually experiences: the play area belongs to
    the building and the fasting menu to the kitchen, and somebody asking for
    both does not care which record holds which.
    """
    venue = getattr(experience, "venue", None)
    return normalise(
        list(getattr(experience, "suitability", None) or [])
        + list(getattr(venue, "facilities", None) or [])
    )


class Assessment:
    """How a listing stands against what somebody asked for.

    Three outcomes, kept apart on purpose. `met` is asserted. `missing` is
    asserted by nobody - unknown, not refused - and is the set the reply has to
    hedge about rather than deny. `conflicts` is for a claim actively
    contradicted by another; nothing produces one yet, and it exists as a field
    so that adding a mutually-exclusive pair later does not have to be squeezed
    into `missing`, where "we do not know" and "definitely not" would become
    indistinguishable.
    """

    __slots__ = ("met", "missing", "conflicts")

    def __init__(self, met: set[str], missing: set[str], conflicts: set[str]) -> None:
        self.met = met
        self.missing = missing
        self.conflicts = conflicts

    @property
    def is_fully_met(self) -> bool:
        return not self.missing and not self.conflicts

    @property
    def is_safe(self) -> bool:
        """Whether nothing safety-critical is unverified.

        The gate for a hard requirement. Unknown counts as unsafe here, which is
        the whole point: "we have no idea whether this kitchen can do nut-free"
        must not reach somebody who said they have a nut allergy.
        """
        return not (self.missing | self.conflicts) & SAFETY_CRITICAL

    def score(self, wanted: int) -> float:
        """0-1 share of what was asked for that this listing actually claims."""
        if wanted <= 0:
            return 0.5
        return len(self.met) / wanted


def assess(experience, wanted: list[str] | set[str] | None) -> Assessment:
    """Compare a listing's claims against a request."""
    request = set(normalise(wanted))
    if not request:
        return Assessment(met=set(), missing=set(), conflicts=set())

    claimed = set(effective(experience))
    met = {
        slug
        for slug in request
        if slug in claimed or (claimed & SATISFIED_BY.get(slug, frozenset()))
    }
    return Assessment(met=met, missing=request - met, conflicts=set())


# IMPLIES, inverted: for each slug, the claims whose presence satisfies a request
# for it. Built once at import rather than recomputed per listing per constraint,
# which is a loop that runs over every candidate in every ranked query.
SATISFIED_BY: dict[str, frozenset[str]] = {
    slug: frozenset(
        source for source, implied in IMPLIES.items() if slug in implied
    )
    for slug in ALL
}


__all__ = [
    "ACCESS",
    "ALL",
    "COMFORT",
    "DIETARY",
    "FAMILY",
    "LABELS",
    "PRACTICAL",
    "SAFETY_CRITICAL",
    "Assessment",
    "assess",
    "describe",
    "effective",
    "expand",
    "label",
    "normalise",
]
